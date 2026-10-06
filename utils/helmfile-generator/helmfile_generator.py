#!/usr/bin/env python3
"""Generate a Helmfile from Compose services and explicit chart conventions."""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import jinja2
import yaml
from tqdm import tqdm


DEFAULT_ENV_VARS = {
    "DISPLAY",
    "DOCKER_GID",
    "DOCKER_UID",
    "LANG",
    "LANGUAGE",
    "LC_ALL",
    "NVIDIA_DRIVER_CAPABILITIES",
    "NVIDIA_VISIBLE_DEVICES",
    "__NV_PRIME_RENDER_OFFLOAD",
    "__GLX_VENDOR_LIBRARY_NAME",
    "XAUTHORITY",
}

IGNORED_VOLUME_TARGETS = {"/etc/localtime", "/etc/timezone", "/dev/null"}


class ConversionError(RuntimeError):
    """Raised when conversion cannot produce a complete output."""


class ChartUnavailableError(ConversionError):
    """Raised when no OCI chart exists for a derived chart reference."""


class ComposeSourceLoader(yaml.SafeLoader):
    """Read source fields inspected alongside the resolved Compose config."""


def _construct_compose_override(loader: ComposeSourceLoader, node: yaml.Node) -> Any:
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    return loader.construct_scalar(node)


ComposeSourceLoader.add_constructor("!override", _construct_compose_override)


@dataclass(frozen=True)
class ChartRef:
    """Resolved OCI Helm chart reference."""

    url: str
    version: str


@dataclass
class ConvertedService:
    """Helmfile and values representation of one Compose service."""

    name: str
    release_name: str
    chart: ChartRef
    values_dir: str
    profiles: tuple[str, ...] = ()
    values: dict[str, Any] = field(default_factory=dict)
    param_source: Path | None = None
    param_filename: str | None = None
    config_sources: dict[str, Path] = field(default_factory=dict)
    file_sources: dict[str, Path] = field(default_factory=dict)
    directory_sources: dict[str, Path] = field(default_factory=dict)
    runtime_environment: dict[str, str] = field(default_factory=dict)
    runtime_command: list[str] | None = None
    runtime_args: list[str] | None = None


class ComposeToHelmfileConverter:
    """Convert all services from a resolved Compose project."""

    def __init__(
        self,
        compose_file: Path,
        profiles: list[str],
        output_dir: Path,
        verbose: bool = False,
        env_file: Path | None = None,
        chart_map: Path | None = None,
        active_profiles_only: bool = False,
    ) -> None:
        self.compose_file = compose_file.resolve()
        self.compose_dir = self.compose_file.parent
        self.env_file = (env_file.resolve() if env_file else self.compose_dir / ".env")
        self.profiles = profiles
        self.active_profiles_only = active_profiles_only
        self.output_dir = output_dir.resolve()
        self.verbose = verbose
        self.chart_values_cache: dict[tuple[str, str], dict[str, Any]] = {}
        self.raw_services: dict[str, dict[str, Any]] = {}
        self.raw_service_sources: dict[str, Path] = {}
        self.source_service_counts: dict[Path, int] = {}
        self.chart_map: dict[str, Any] = {}
        if chart_map:
            if not chart_map.is_file():
                raise ConversionError(f"chart map not found: {chart_map}")
            loaded_map = yaml.safe_load(chart_map.read_text()) or {}
            if not isinstance(loaded_map, dict):
                raise ConversionError("chart map must be a mapping")
            self.chart_map = loaded_map
        self.service_mappings = self.chart_map.get("services", {})
        if not isinstance(self.service_mappings, dict):
            raise ConversionError("services must be a mapping")
        for name, mapping in self.service_mappings.items():
            if (
                not isinstance(mapping, dict)
                or bool(mapping.get("chart")) != bool(mapping.get("version"))
            ):
                raise ConversionError(
                    f"{name}: chart and version must be provided together"
                )
        excluded_profiles = self.chart_map.get("excluded_profiles", [])
        if (
            not isinstance(excluded_profiles, list)
            or not all(isinstance(profile, str) for profile in excluded_profiles)
        ):
            raise ConversionError("excluded_profiles must be a list of profile names")
        self.excluded_profiles = frozenset(excluded_profiles)
        for key in ("ignored_environment", "ignored_volume_names"):
            names = self.chart_map.get(key, [])
            if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
                raise ConversionError(f"{key} must be a list of names")
        self.ignored_environment = DEFAULT_ENV_VARS | set(self.chart_map.get("ignored_environment", []))
        self.ignored_volume_names = set(self.chart_map.get("ignored_volume_names", []))
        self.deployment = self.chart_map.get("deployment", {})
        if not isinstance(self.deployment, dict):
            raise ConversionError("deployment must be a mapping")
        for key in ("name_prefix_env", "node_env", "host_path_root_env"):
            value = self.deployment.get(key)
            if value is not None and (not isinstance(value, str) or not value):
                raise ConversionError(f"deployment.{key} must be an environment variable name")
        self.chart_version_suffix = self.chart_map.get("chart_version_suffix", "")
        if not isinstance(self.chart_version_suffix, str):
            raise ConversionError("chart_version_suffix must be a string")
        self.values_root = self.chart_map.get("values_root", "")
        if not isinstance(self.values_root, str):
            raise ConversionError("values_root must be a string")
        self.parameter_file = self.chart_map.get("parameter_file")
        if self.parameter_file is not None:
            required_keys = {
                "yaml_marker",
                "value_key",
                "mount_path_value_key",
                "output_filename",
            }
            if (
                not isinstance(self.parameter_file, dict)
                or set(self.parameter_file) != required_keys
                or any(
                    not isinstance(self.parameter_file[key], str)
                    or not self.parameter_file[key]
                    for key in required_keys
                )
            ):
                raise ConversionError(
                    "parameter_file must contain yaml_marker, value_key, "
                    "mount_path_value_key, and output_filename strings"
                )
        for service_name, mapping in self.service_mappings.items():
            value_rules = mapping.get("value_replacements", {})
            if not isinstance(value_rules, dict):
                raise ConversionError(f"{service_name}: value_replacements must be a mapping")
            config_rules = mapping.get("config_replacements", {})
            if not isinstance(config_rules, dict):
                raise ConversionError(
                    f"{service_name}: config_replacements must be a mapping"
                )
            for rules in [
                *value_rules.values(),
                *config_rules.values(),
                mapping.get("param_replacements", []),
            ]:
                if not isinstance(rules, list) or any(
                    not isinstance(rule, dict)
                    or not isinstance(rule.get("find"), str)
                    or not isinstance(rule.get("replace"), str)
                    for rule in rules
                ):
                    raise ConversionError(f"{service_name}: replacements must contain find/replace strings")
        volume_policies = self.chart_map.get("volume_policies", {})
        if not isinstance(volume_policies, dict):
            raise ConversionError("volume_policies must be a mapping")
        for service_name, mounts in volume_policies.items():
            if not isinstance(mounts, dict) or any(
                not isinstance(target, str) or not target.startswith("/")
                or mode not in {"copy", "hostPath", "omit"}
                for target, mode in mounts.items()
            ):
                raise ConversionError(
                    f"{service_name}: volume_policies must map absolute mount paths "
                    "to copy, hostPath, or omit"
                )

        template_dir = Path(__file__).parent / "templates"
        self.jinja_env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(template_dir)),
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=True,
        )
        self.jinja_env.filters["yaml_scalar"] = self._yaml_scalar
        self.jinja_env.filters["go_string"] = self._go_string

    def _volume_policy(self, service_name: str, target: str) -> str | None:
        return self.chart_map.get("volume_policies", {}).get(service_name, {}).get(target)

    @staticmethod
    def _merge_values(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
        result = copy.deepcopy(base)
        for key, value in override.items():
            if isinstance(value, dict) and isinstance(result.get(key), dict):
                result[key] = ComposeToHelmfileConverter._merge_values(
                    result[key], value
                )
            else:
                result[key] = copy.deepcopy(value)
        return result

    @staticmethod
    def _apply_replacements(content: str, rules: list[dict[str, str]]) -> str:
        for rule in rules:
            content = content.replace(rule["find"], rule["replace"])
        return content

    @staticmethod
    def _contains_host_expression(value: Any) -> bool:
        return isinstance(value, str) and re.search(r"(?<!\$)\$\{", value) is not None

    @staticmethod
    def _go_string(value: str) -> str:
        return json.dumps(value, ensure_ascii=False)

    def _compose_expression(self, value: str) -> str:
        """Translate Compose interpolation into a Helmfile Go-template expression."""
        parts: list[str] = []
        literal: list[str] = []
        index = 0

        def flush_literal() -> None:
            if literal:
                parts.append(self._go_string("".join(literal).replace("$$", "$")))
                literal.clear()

        while index < len(value):
            if value.startswith("$${", index):
                literal.append("${")
                index += 3
                continue
            if not value.startswith("${", index):
                literal.append(value[index])
                index += 1
                continue
            flush_literal()
            depth = 1
            end = index + 2
            while end < len(value) and depth:
                if value.startswith("${", end):
                    depth += 1
                    end += 2
                    continue
                if value[end] == "}":
                    depth -= 1
                end += 1
            if depth:
                raise ConversionError(f"unterminated Compose expression: {value}")
            body = value[index + 2:end - 1]
            match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)(:?[-+?])?(.*)", body, re.S)
            if not match:
                raise ConversionError(f"unsupported Compose expression: ${{{body}}}")
            name, operator, word = match.groups()
            variable = f'(env {self._go_string(name)})'
            fallback = self._compose_expression(word) if word else '""'
            if operator in (":-", "-"):
                expression = f"(default {fallback} {variable})"
            elif operator in (":+", "+"):
                expression = f"(ternary {fallback} \"\" (not (empty {variable})))"
            elif operator in (":?", "?"):
                message = word or f"{name} is required"
                expression = f"(required {self._go_string(message)} {variable})"
            else:
                expression = variable
            parts.append(expression)
            index = end
        flush_literal()
        if not parts:
            return '""'
        if len(parts) == 1:
            return parts[0]
        return '(printf "' + "%s" * len(parts) + '" ' + " ".join(parts) + ")"

    @staticmethod
    def _environment_mapping(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return dict(value)
        result: dict[str, Any] = {}
        if isinstance(value, list):
            for item in value:
                if not isinstance(item, str):
                    continue
                key, separator, item_value = item.partition("=")
                result[key] = item_value if separator else None
        return result

    @classmethod
    def _merge_raw_service(cls, base: dict[str, Any], child: dict[str, Any]) -> dict[str, Any]:
        result = copy.deepcopy(base)
        for key, value in child.items():
            if key == "extends":
                continue
            if key == "environment":
                result[key] = {
                    **cls._environment_mapping(result.get(key)),
                    **cls._environment_mapping(value),
                }
            elif isinstance(value, dict) and isinstance(result.get(key), dict):
                result[key] = cls._merge_raw_service(result[key], value)
            else:
                result[key] = copy.deepcopy(value)
        return result

    def _load_raw_service(
        self,
        compose_path: Path,
        service_name: str,
        seen: set[tuple[Path, str]] | None = None,
    ) -> dict[str, Any]:
        compose_path = compose_path.resolve()
        seen = set() if seen is None else seen
        marker = (compose_path, service_name)
        if marker in seen:
            raise ConversionError(f"cyclic Compose extends for {service_name}")
        seen.add(marker)
        try:
            document = yaml.load(
                compose_path.read_text(encoding="utf-8"), Loader=ComposeSourceLoader
            ) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise ConversionError(f"cannot read Compose source {compose_path}: {exc}") from exc
        services = document.get("services", {}) if isinstance(document, dict) else {}
        service = services.get(service_name, {}) if isinstance(services, dict) else {}
        if not isinstance(service, dict):
            return {}
        parent: dict[str, Any] = {}
        extends = service.get("extends")
        if isinstance(extends, dict) and extends.get("service"):
            parent_path = compose_path.parent / str(extends.get("file", compose_path.name))
            parent = self._load_raw_service(parent_path, str(extends["service"]), seen)
        return self._merge_raw_service(parent, service)

    def _discover_raw_services(self) -> dict[str, dict[str, Any]]:
        discovered: dict[str, dict[str, Any]] = {}
        visited: set[Path] = set()

        def visit(path: Path) -> None:
            path = path.resolve()
            if path in visited:
                return
            visited.add(path)
            try:
                document = yaml.load(
                    path.read_text(encoding="utf-8"), Loader=ComposeSourceLoader
                ) or {}
            except (OSError, yaml.YAMLError) as exc:
                raise ConversionError(f"cannot read Compose source {path}: {exc}") from exc
            if not isinstance(document, dict):
                return
            includes = document.get("include", [])
            if not isinstance(includes, list):
                includes = [includes]
            for include in includes:
                include_path = include.get("path") if isinstance(include, dict) else include
                if isinstance(include_path, list):
                    for item in include_path:
                        visit(path.parent / str(item))
                elif include_path:
                    visit(path.parent / str(include_path))
            services = document.get("services", {})
            if not isinstance(services, dict):
                return
            self.source_service_counts[path] = len(services)
            for name in services:
                raw = self._load_raw_service(path, str(name))
                discovered[str(name)] = self._merge_raw_service(discovered.get(str(name), {}), raw)
                self.raw_service_sources[str(name)] = path

        visit(self.compose_file)
        return discovered

    def _compose_values_location(
        self, service_name: str, release_name: str
    ) -> str:
        source = self.raw_service_sources.get(service_name)
        if source is None:
            return f"other/{release_name}"
        try:
            parent = source.parent.relative_to(self.compose_dir)
        except ValueError:
            return f"other/{release_name}"
        if parent == Path("."):
            return release_name
        if self.source_service_counts.get(source) == 1:
            return parent.as_posix()
        return (parent / release_name).as_posix()

    @staticmethod
    def _sanitize_name(name: str) -> str:
        value = re.sub(r"[_.]", "-", name.lower())
        value = re.sub(r"[^a-z0-9-]", "", value)
        value = re.sub(r"-+", "-", value).strip("-")
        return value or "service"

    def _release_name(self, service_name: str) -> str:
        short_name = self._sanitize_name(service_name.split(".")[-1])
        collisions = sum(
            self._sanitize_name(name.split(".")[-1]) == short_name
            for name in self.raw_services
        )
        return self._sanitize_name(service_name) if collisions > 1 else short_name

    @staticmethod
    def _yaml_scalar(value: Any) -> str:
        rendered = yaml.safe_dump(
            value,
            default_flow_style=True,
            allow_unicode=True,
            sort_keys=False,
        ).strip()
        if rendered.endswith("\n..."):
            rendered = rendered[:-4]
        return rendered

    @staticmethod
    def _coerce_scalar(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        lowered = value.lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
        if lowered in {"null", "~"}:
            return None
        if re.fullmatch(r"[-+]?(?:0|[1-9][0-9]*)", value):
            return int(value)
        if re.fullmatch(r"[-+]?(?:[0-9]+\.[0-9]*|[0-9]*\.[0-9]+)", value):
            return float(value)
        return value

    @staticmethod
    def _normalize_command(value: Any) -> list[str]:
        if value is None:
            return []
        items = value if isinstance(value, list) else [value]
        normalized: list[str] = []
        for item in items:
            text = (
                str(item)
                .replace("$$", "$")
                .replace("\r\n", "\n")
                .replace("\r", "\n")
            )
            text = "\n".join(line.rstrip() for line in text.splitlines()).strip()
            if text:
                normalized.append(text)
        return normalized

    @staticmethod
    def _normalize_raw_command(value: Any) -> list[str]:
        if value is None:
            return []
        items = value if isinstance(value, list) else [value]
        normalized: list[str] = []
        for item in items:
            text = str(item).replace("\r\n", "\n").replace("\r", "\n")
            text = "\n".join(line.rstrip() for line in text.splitlines()).strip()
            if text:
                normalized.append(text)
        return normalized

    @staticmethod
    def _commands_equal(left: list[str], right: list[str]) -> bool:
        def comparable(items: list[str]) -> list[str]:
            return [re.sub(r"\s+", " ", item).strip() for item in items]

        return comparable(left) == comparable(right)

    def _compose_command(
        self, compose_file: Path, profiles: list[str] | None = None
    ) -> list[str]:
        command = [
            "docker",
            "compose",
            "--env-file",
            str(self.env_file),
            "-f",
            str(compose_file),
        ]
        for profile in self.profiles if profiles is None else profiles:
            command.extend(["--profile", profile])
        command.extend(["config", "--format", "yaml"])
        return command

    def _resolve_compose(
        self, compose_file: Path, profiles: list[str] | None = None
    ) -> dict[str, Any]:
        try:
            result = subprocess.run(
                self._compose_command(compose_file, profiles),
                cwd=self.compose_dir,
                capture_output=True,
                text=True,
                check=True,
            )
        except FileNotFoundError as exc:
            raise ConversionError("docker compose is not available") from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or str(exc)).strip()
            raise ConversionError(
                f"failed to resolve {compose_file}: {detail}"
            ) from exc

        try:
            data = yaml.safe_load(result.stdout)
        except yaml.YAMLError as exc:
            raise ConversionError(
                f"docker compose returned invalid YAML for {compose_file}: {exc}"
            ) from exc
        if not isinstance(data, dict) or not isinstance(data.get("services"), dict):
            raise ConversionError(
                f"docker compose returned no services for {compose_file}"
            )
        return data

    def _chart_ref(self, service_name: str, image: str) -> ChartRef:
        mapping = self.service_mappings.get(service_name)
        if mapping and mapping.get("chart"):
            return ChartRef(
                mapping["chart"], str(mapping["version"]),
            )
        match = re.fullmatch(
            r"ghcr\.io/(?P<owner>[^/]+)/(?P<repository>[^:@]+):"
            r"(?P<tag>[^@]+)(?:@.+)?",
            image,
        )
        if not match:
            raise ConversionError(
                f"{service_name}: unsupported image '{image}'; "
                "add an explicit chart mapping"
            )

        tag = match.group("tag")
        version_match = re.match(r"v?(\d+\.\d+\.\d+)", tag)
        version = (
            version_match.group(1)
            if version_match
            else (tag[1:] if tag.startswith("v") else tag)
        )
        if not version:
            raise ConversionError(
                f"{service_name}: image '{image}' has no usable version tag"
            )
        chart_name = self._sanitize_name(match.group("repository"))
        return ChartRef(
            url=(
                f"oci://ghcr.io/{match.group('owner')}/{match.group('repository')}"
                f"/helm/{chart_name}"
            ),
            version=f"{version}{self.chart_version_suffix}",
        )

    def _chart_defaults(self, chart: ChartRef) -> dict[str, Any]:
        key = (chart.url, chart.version)
        if key in self.chart_values_cache:
            return self.chart_values_cache[key]
        command = [
            "helm",
            "show",
            "values",
            chart.url,
            "--version",
            chart.version,
        ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=True,
            )
        except FileNotFoundError as exc:
            raise ConversionError("helm is not available") from exc
        except subprocess.CalledProcessError as first_exc:
            with tempfile.TemporaryDirectory(
                prefix="compose-to-helmfile-helm-"
            ) as temp_dir:
                registry_config = Path(temp_dir) / "registry.json"
                registry_config.write_text(
                    json.dumps({"auths": {}}),
                    encoding="utf-8",
                )
                environment = os.environ.copy()
                environment["HELM_REGISTRY_CONFIG"] = str(registry_config)
                try:
                    result = subprocess.run(
                        command,
                        capture_output=True,
                        text=True,
                        check=True,
                        env=environment,
                    )
                except subprocess.CalledProcessError as retry_exc:
                    detail = (
                        retry_exc.stderr
                        or retry_exc.stdout
                        or first_exc.stderr
                        or first_exc.stdout
                        or str(retry_exc)
                    ).strip()
                    if re.search(
                        r"(?:manifest unknown|name unknown|not found)",
                        detail,
                        flags=re.IGNORECASE,
                    ):
                        raise ChartUnavailableError(
                            f"no chart available at {chart.url} "
                            f"{chart.version}; add an explicit chart mapping"
                        ) from retry_exc
                    raise ConversionError(
                        f"failed to load values for {chart.url} "
                        f"{chart.version}: {detail}"
                    ) from retry_exc

        try:
            values = yaml.safe_load(result.stdout)
        except yaml.YAMLError as exc:
            raise ConversionError(
                f"chart {chart.url} {chart.version} returned invalid values YAML: {exc}"
            ) from exc
        defaults = values.get(self.values_root) if self.values_root else values
        if not isinstance(defaults, dict):
            raise ConversionError(
                f"chart {chart.url} {chart.version} has no mapping at values_root"
            )
        self.chart_values_cache[key] = defaults
        return defaults

    @staticmethod
    def _detect_gpu(service: dict[str, Any]) -> bool:
        devices = (
            service.get("deploy", {})
            .get("resources", {})
            .get("reservations", {})
            .get("devices", [])
        )
        return any(
            isinstance(device, dict)
            and device.get("driver") == "nvidia"
            and "gpu" in device.get("capabilities", [])
            for device in devices
        )

    @staticmethod
    def _detect_x11(service: dict[str, Any]) -> bool:
        environment = service.get("environment", {})
        if isinstance(environment, dict) and (
            "DISPLAY" in environment or "XAUTHORITY" in environment
        ):
            return True
        for volume in service.get("volumes", []):
            if not isinstance(volume, dict):
                continue
            target = str(volume.get("target", ""))
            if "/tmp/.X11-unix" in target or ".Xauthority" in target:
                return True
        return False

    def _environment(
        self,
        service_name: str,
        service: dict[str, Any],
        defaults: dict[str, Any],
        ignored_keys: set[str] | None = None,
    ) -> dict[str, Any]:
        raw_environment = service.get("environment", {})
        if not isinstance(raw_environment, dict):
            return {}
        ignored_keys = ignored_keys or set()
        chart_environment = defaults.get("env", {})
        if not isinstance(chart_environment, dict):
            chart_environment = {}

        source_environment = self._environment_mapping(
            self.raw_services.get(service_name, {}).get("environment")
        )
        ordered_keys = list(source_environment) + [
            key for key in raw_environment if key not in source_environment
        ]
        result: dict[str, Any] = {}
        for key in ordered_keys:
            if (
                key not in raw_environment
                or key in self.ignored_environment
                or key in ignored_keys
                or raw_environment[key] is None
            ):
                continue
            value = self._coerce_scalar(raw_environment[key])
            default_value = self._coerce_scalar(chart_environment.get(key))
            source_value = source_environment.get(key)
            if (
                key in chart_environment
                and value == default_value
                and not (
                    isinstance(source_value, str)
                    and self._contains_host_expression(source_value)
                )
            ):
                continue
            result[key] = value
        return result

    @staticmethod
    def _ports(
        service: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        ports: list[dict[str, Any]] = []
        expose: list[dict[str, Any]] = []
        for item in service.get("ports", []):
            if not isinstance(item, dict) or item.get("target") is None:
                continue
            entry = {
                "targetPort": int(item["target"]),
                "protocol": str(item.get("protocol", "tcp")).upper(),
            }
            if item.get("published") is None:
                expose.append(entry)
            else:
                entry["nodePort"] = int(item["published"])
                ports.append(entry)
        for item in service.get("expose", []):
            if isinstance(item, int):
                expose.append({"targetPort": item, "protocol": "TCP"})
            elif isinstance(item, str):
                port_text, _, protocol = item.partition("/")
                expose.append(
                    {
                        "targetPort": int(port_text),
                        "protocol": (protocol or "tcp").upper(),
                    }
                )
        ports.sort(
            key=lambda item: (
                item["nodePort"],
                item["targetPort"],
                item["protocol"],
            )
        )
        expose.sort(key=lambda item: (item["targetPort"], item["protocol"]))
        return ports, expose

    @staticmethod
    def _normalize_port_values(
        value: Any, node_port: bool
    ) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        normalized: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict) or item.get("targetPort") is None:
                continue
            entry = {
                "targetPort": int(item["targetPort"]),
                "protocol": str(item.get("protocol", "TCP")).upper(),
            }
            if node_port and item.get("nodePort") is not None:
                entry["nodePort"] = int(item["nodePort"])
            normalized.append(entry)
        return sorted(
            normalized,
            key=lambda item: (
                item.get("nodePort", 0),
                item["targetPort"],
                item["protocol"],
            ),
        )

    def _depends_on(self, service: dict[str, Any]) -> list[dict[str, Any]]:
        """Translate Compose service dependencies to TCP readiness checks.

        Kubernetes does not provide Compose's startup ordering. The target
        chart implements the equivalent through its ``dependsOn`` value. A
        dependency without a TCP port cannot be represented and is omitted.
        """
        dependencies = service.get("depends_on", {})
        if isinstance(dependencies, list):
            names = [str(name) for name in dependencies]
        elif isinstance(dependencies, dict):
            names = [str(name) for name in dependencies]
        else:
            return []

        checks: list[dict[str, Any]] = []
        for name in sorted(set(names)):
            dependency = self.raw_services.get(name, {})
            port_env = self.service_mappings.get(name, {}).get("dependency_port_env")
            if port_env:
                configured_port = self._environment_mapping(
                    dependency.get("environment")
                ).get(port_env)
                if isinstance(configured_port, str) and self._contains_host_expression(configured_port):
                    checks.append({
                        "host": self._sanitize_name(name),
                        "port": configured_port,
                    })
                    continue
            image = str(dependency.get("image", ""))
            defaults = self._chart_defaults(self._chart_ref(name, image))
            _, compose_expose = self._ports(dependency)
            chart_expose = self._normalize_port_values(
                defaults.get("expose"), node_port=False
            )
            exposed = compose_expose or chart_expose
            tcp_ports = [
                int(port["targetPort"])
                for port in exposed
                if port.get("protocol", "TCP").upper() == "TCP"
            ]
            if tcp_ports:
                checks.append({"host": self._sanitize_name(name), "port": min(tcp_ports)})
        return checks

    @staticmethod
    def _contains_yaml_key(path: Path, marker: str) -> bool:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            return False

        def visit(value: Any) -> bool:
            if isinstance(value, dict):
                return marker in value or any(visit(child) for child in value.values())
            if isinstance(value, list):
                return any(visit(child) for child in value)
            return False

        return visit(data)

    def _host_root_expression(self, source: Path) -> str:
        source = source.resolve()
        try:
            relative = source.relative_to(self.compose_dir)
        except ValueError:
            return self._go_string(str(source))
        root_env = self.deployment.get("host_path_root_env")
        if not root_env:
            return self._go_string(str(source))
        if relative == Path("."):
            return f'(requiredEnv {self._go_string(root_env)})'
        return (
            '(printf "%s/%s" '
            f'(requiredEnv {self._go_string(root_env)}) '
            f'{self._go_string(relative.as_posix())} | clean)'
        )

    def _host_path_value(self, source: Path) -> str:
        if not source.resolve().is_relative_to(self.compose_dir) or not self.deployment.get("host_path_root_env"):
            return str(source)
        expression = self._host_root_expression(source)
        return f'{{{{ {expression} | quote }}}}'

    def _volumes(
        self,
        service_name: str,
        service: dict[str, Any],
        defaults: dict[str, Any],
        values_dir: str,
        runtime_targets: set[str] | None = None,
    ) -> tuple[
        list[dict[str, Any]], Path | None, str | None, dict[str, Path],
        dict[str, Path], dict[str, Path]
    ]:
        volumes: list[dict[str, Any]] = []
        param_source: Path | None = None
        param_filename: str | None = None
        config_sources: dict[str, Path] = {}
        file_sources: dict[str, Path] = {}
        directory_sources: dict[str, Path] = {}

        for volume in service.get("volumes", []):
            if not isinstance(volume, dict):
                continue
            volume_type = volume.get("type")
            if volume_type == "image":
                source_value = volume.get("source")
                target_value = volume.get("target")
                image_config = volume.get("image", {})
                if not isinstance(source_value, str) or not source_value.strip():
                    raise ConversionError(
                        f"{service_name}: image volume has no source"
                    )
                if not isinstance(target_value, str) or not target_value.strip():
                    raise ConversionError(
                        f"{service_name}: image volume has no target"
                    )
                source = source_value.strip()
                target = target_value.strip()
                if not target.startswith("/"):
                    raise ConversionError(
                        f"{service_name}: image volume target must be absolute: "
                        f"{target}"
                    )
                if volume.get("read_only") is False:
                    raise ConversionError(
                        f"{service_name}: image volumes must be read-only"
                    )
                if not isinstance(image_config, dict):
                    raise ConversionError(
                        f"{service_name}: image volume configuration is invalid"
                    )
                sub_path_value = image_config.get("subpath", "")
                if not isinstance(sub_path_value, str):
                    raise ConversionError(
                        f"{service_name}: image volume subpath is invalid"
                    )
                sub_path = sub_path_value.strip()
                path_parts = PurePosixPath(sub_path).parts
                if sub_path and (
                    PurePosixPath(sub_path).is_absolute() or ".." in path_parts
                ):
                    raise ConversionError(
                        f"{service_name}: image volume subpath must be relative: "
                        f"{sub_path}"
                    )
                image_volume: dict[str, Any] = {
                    "mountPath": target,
                    "image": source,
                }
                if sub_path:
                    image_volume["imagePath"] = sub_path
                volumes.append(image_volume)
                continue
            if volume_type != "bind":
                continue
            source = Path(str(volume.get("source", "")))
            target = str(volume.get("target", ""))
            if source.resolve() == self.env_file:
                raise ConversionError(
                    f"{service_name}: refusing to export the Compose environment file"
                )
            policy = self._volume_policy(service_name, target)
            if policy == "omit":
                continue
            if (
                target in (runtime_targets or set())
                or target in IGNORED_VOLUME_TARGETS
                or source.name in self.ignored_volume_names
                or "/tmp/.X11-unix" in target
                or ".Xauthority" in target
            ):
                continue
            if not source.exists():
                raise ConversionError(
                    f"{service_name}: mounted source does not exist: {source}"
                )
            if policy == "hostPath":
                volumes.append({"mountPath": target, "hostPath": self._host_path_value(source)})
                continue
            if (
                self.parameter_file
                and source.is_file()
                and source.suffix.lower() in {".yml", ".yaml"}
                and self._contains_yaml_key(
                    source, self.parameter_file["yaml_marker"]
                )
            ):
                if param_source is not None:
                    raise ConversionError(
                        f"{service_name}: multiple parameter files are mounted"
                    )
                param_source = source
                param_filename = self.parameter_file["output_filename"]
                continue
            # Small text files become ConfigMaps. Other fixed files are copied
            # beside the values and mounted from that generated location.
            if source.is_file() and source.stat().st_size < 256 * 1024:
                try:
                    content = source.read_text(encoding="utf-8")
                    if "\x00" not in content:
                        filename = source.name
                        existing = config_sources.get(filename) or file_sources.get(filename)
                        if existing is not None and existing != source:
                            raise ConversionError(
                                f"{service_name}: configuration files share the name "
                                f"'{filename}'"
                            )
                        config_sources[filename] = source
                        volumes.append({"mountPath": target, "dataFile": filename})
                        continue
                except UnicodeDecodeError:
                    pass
            if source.is_file():
                filename = source.name
                existing = config_sources.get(filename) or file_sources.get(filename)
                if existing is not None and existing != source:
                    raise ConversionError(
                        f"{service_name}: mounted files share the name '{filename}'"
                    )
                file_sources[filename] = source
                destination = self.output_dir / values_dir / filename
                volumes.append({
                    "mountPath": target,
                    "hostPath": self._host_path_value(destination),
                })
                continue
            if source.is_dir():
                dirname = source.name
                existing = directory_sources.get(dirname)
                if existing is not None and existing != source:
                    raise ConversionError(
                        f"{service_name}: mounted directories share the name '{dirname}'"
                    )
                directory_sources[dirname] = source
                destination = self.output_dir / values_dir / dirname
                volumes.append({
                    "mountPath": target,
                    "hostPath": self._host_path_value(destination),
                })
                continue
            raise ConversionError(
                f"{service_name}: mounted source is neither file nor directory: {source}"
            )

        if volumes == defaults.get("volumes", []):
            volumes = []
        return (volumes, param_source, param_filename, config_sources,
                file_sources, directory_sources)

    @staticmethod
    def _split_volume_spec(value: str) -> list[str]:
        parts: list[str] = []
        start = index = depth = 0
        while index < len(value):
            if value.startswith("${", index):
                depth += 1
                index += 2
                continue
            if value[index] == "}" and depth:
                depth -= 1
            elif value[index] == ":" and not depth:
                parts.append(value[start:index])
                start = index + 1
            index += 1
        parts.append(value[start:])
        return parts

    def _runtime_volumes(
        self, service_name: str, service: dict[str, Any]
    ) -> list[dict[str, str]]:
        raw_service = self.raw_services.get(service_name, {})
        source_file = self.raw_service_sources.get(service_name)
        if source_file is None:
            return []
        volumes: list[dict[str, str]] = []
        # Compose uses /dev/null as the unset branch of this optional bind.
        # Keep the mount selectable when Helmfile runs with a different env.
        pattern = re.compile(
            r"\$\{(?P<variable>[A-Za-z_][A-Za-z0-9_]*):\+(?P<prefix>[^}]*)\}"
            r"\$\{(?P=variable):-/dev/null:/dev/null\}"
            r"\$\{(?P=variable):\+:(?P<target>/[^}]+)\}"
        )
        for raw_volume in raw_service.get("volumes", []):
            if not isinstance(raw_volume, str):
                continue
            match = pattern.fullmatch(raw_volume)
            if match is not None:
                if self._volume_policy(service_name, match.group("target")) == "omit":
                    continue
                variable = match.group("variable")
                root = self._host_root_expression(
                    source_file.parent / match.group("prefix")
                )
                volumes.append({
                    "mountPath": match.group("target"),
                    "hostPath": (
                        '{{ (ternary (printf "%s/%s" '
                        f'{root} (env {self._go_string(variable)}) | clean) '
                        f'"" (ne (env {self._go_string(variable)}) "")) | quote }}}}'
                    ),
                })
                continue
            parts = self._split_volume_spec(raw_volume)
            if len(parts) < 2 or not self._contains_host_expression(parts[0]):
                continue
            target = parts[1]
            if self._volume_policy(service_name, target) == "omit":
                continue
            if not target.startswith("/") or self._contains_host_expression(target):
                continue
            resolved = next(
                (volume for volume in service.get("volumes", [])
                 if isinstance(volume, dict) and volume.get("target") == target),
                None,
            )
            if resolved is None or resolved.get("type") != "bind":
                continue
            source = Path(str(resolved.get("source", "")))
            if not source.exists():
                continue
            if source.is_file() and source.stat().st_size < 256 * 1024:
                try:
                    if "\x00" not in source.read_text(encoding="utf-8"):
                        continue
                except UnicodeDecodeError:
                    pass
            prefix, _, dynamic_source = parts[0].partition("${")
            if prefix and not prefix.startswith("/") and prefix.endswith("/"):
                root = self._host_root_expression(source_file.parent / prefix)
                dynamic = self._compose_expression("${" + dynamic_source)
                expression = f'(printf "%s/%s" {root} {dynamic} | clean)'
            else:
                raw_source = self._compose_expression(parts[0])
                root = self._host_root_expression(source_file.parent)
                expression = (
                    f'(ternary ({raw_source} | clean) '
                    f'(printf "%s/%s" {root} {raw_source} | clean) '
                    f'(hasPrefix "/" {raw_source}))'
                )
            volumes.append({
                "mountPath": target,
                "hostPath": f'{{{{ {expression} | quote }}}}',
            })
        return volumes

    def _convert_service(
        self, service_name: str, service: dict[str, Any]
    ) -> ConvertedService:
        image = str(service.get("image", ""))
        release_name = self._release_name(service_name)
        chart = self._chart_ref(service_name, image)
        values_dir = self._compose_values_location(service_name, release_name)
        defaults = self._chart_defaults(chart)
        values: dict[str, Any] = {}

        # Keep resource names unique when several releases share one chart.
        if release_name != defaults.get("name"):
            values["name"] = release_name

        if image and image != defaults.get("image"):
            values["image"] = image

        compose_command = self._normalize_command(service.get("entrypoint"))
        chart_command = self._normalize_command(defaults.get("command"))
        if not self._commands_equal(compose_command, chart_command):
            values["command"] = compose_command

        compose_args = self._normalize_command(service.get("command"))
        chart_args = self._normalize_command(defaults.get("args"))
        if not self._commands_equal(compose_args, chart_args):
            values["args"] = compose_args

        gpu = self._detect_gpu(service)
        if gpu != bool(defaults.get("gpu", False)):
            values["gpu"] = gpu

        x11 = self._detect_x11(service)
        default_x11 = bool(
            defaults.get("x11Display") or defaults.get("x11XauthorityFile")
        )
        if x11 != default_x11:
            values["x11"] = x11

        runtime_volumes = self._runtime_volumes(service_name, service)
        volumes, param_source, param_filename, config_sources, file_sources, directory_sources = self._volumes(
            service_name, service, defaults, values_dir,
            runtime_targets={volume["mountPath"] for volume in runtime_volumes},
        )
        volumes.extend(runtime_volumes)
        environment = self._environment(
            service_name, service, defaults, ignored_keys=set()
        )
        if environment:
            values["env"] = environment
        if volumes:
            values["volumes"] = volumes
        if param_source and param_filename:
            assert self.parameter_file is not None
            values[self.parameter_file["value_key"]] = param_filename
            values[self.parameter_file["mount_path_value_key"]] = next(
                v["target"] for v in service.get("volumes", [])
                if v.get("type") == "bind" and Path(v["source"]) == param_source
            )

        ports, expose = self._ports(service)
        if ports and ports != self._normalize_port_values(
            defaults.get("ports"), node_port=True
        ):
            values["ports"] = ports
        if expose and expose != self._normalize_port_values(
            defaults.get("expose"), node_port=False
        ):
            values["expose"] = expose

        depends_on = self._depends_on(service)
        if depends_on:
            values["dependsOn"] = depends_on

        mapping = self.service_mappings.get(service_name, {})
        value_overrides = mapping.get("values", {})
        if value_overrides:
            if not isinstance(value_overrides, dict):
                raise ConversionError(f"{service_name}: chart values must be a mapping")
            converted_volumes = values.get("volumes", [])
            values = self._merge_values(values, value_overrides)
            if "volumes" in value_overrides:
                override_targets = {
                    volume.get("mountPath")
                    for volume in value_overrides["volumes"]
                }
                values["volumes"].extend(
                    volume for volume in converted_volumes
                    if volume.get("mountPath") not in override_targets
                )
        for volume in values.get("volumes", []):
            host_path = volume.get("hostPath")
            if isinstance(host_path, str) and host_path and not host_path.startswith("/"):
                if not host_path.startswith("{{"):
                    volume["hostPath"] = self._host_path_value(
                        self.compose_dir / host_path
                    )

        raw_service = self.raw_services.get(service_name, {})
        raw_environment = self._environment_mapping(raw_service.get("environment"))
        runtime_environment = {
            key: value
            for key, value in raw_environment.items()
            if (
                key not in self.ignored_environment
                and self._contains_host_expression(value)
            )
        }
        raw_entrypoint = self._normalize_raw_command(raw_service.get("entrypoint"))
        raw_command = self._normalize_raw_command(raw_service.get("command"))

        profiles = tuple(sorted(
            str(profile)
            for profile in raw_service.get("profiles", [])
        ))
        return ConvertedService(
            name=service_name,
            release_name=release_name,
            chart=chart,
            values_dir=values_dir,
            profiles=profiles,
            values=values,
            param_source=param_source,
            param_filename=param_filename,
            config_sources=config_sources,
            file_sources=file_sources,
            directory_sources=directory_sources,
            runtime_environment=runtime_environment,
            runtime_command=(
                raw_entrypoint
                if any(self._contains_host_expression(item) for item in raw_entrypoint)
                else None
            ),
            runtime_args=(
                raw_command
                if any(self._contains_host_expression(item) for item in raw_command)
                else None
            ),
        )

    def _render_helmfile(
        self, services: list[ConvertedService]
    ) -> str:
        template = self.jinja_env.get_template("helmfile.j2")
        return template.render(services=services, prefix_env=self.deployment.get("name_prefix_env"))

    def _render_services(self, services: list[ConvertedService]) -> str:
        template = self.jinja_env.get_template("services.yaml.gotmpl.j2")
        available_profiles = sorted({
            profile for service in services for profile in service.profiles
        })
        default_profiles = [
            profile.strip() for profile in self.profiles if profile.strip()
        ]
        if "*" in default_profiles:
            default_profiles = available_profiles
        return template.render(
            default_profiles=",".join(default_profiles),
            services=services,
        )

    def _render_values(self, service: ConvertedService) -> str:
        values = copy.deepcopy(service.values)
        replacements: dict[str, str] = {}

        def template_value(expression: str, quote: bool = True) -> str:
            marker = f"__HELMFILE_GOTMPL_{len(replacements)}__"
            suffix = " | quote" if quote else ""
            replacements[marker] = f"{{{{ {expression}{suffix} }}}}"
            return marker

        def template_block(expression: str, indent: int) -> str:
            marker = template_value(expression, quote=False)
            replacements[marker] = (
                f"|-\n{{{{ {expression} | indent {indent} }}}}"
            )
            return marker

        def raw_template(value: str) -> str:
            marker = f"__HELMFILE_GOTMPL_{len(replacements)}__"
            replacements[marker] = value
            return marker

        def literal_block(value: str, indent: int) -> str:
            marker = f"__HELMFILE_GOTMPL_{len(replacements)}__"
            prefix = " " * indent
            # Compose's `|` scalar supplies a final newline. Preserve it when
            # the command ends in a shell continuation; using `|-` there turns
            # the final backslash into a literal command-line argument.
            block_header = "|" if value.endswith("\\") else "|-"
            replacements[marker] = block_header + "\n" + "\n".join(
                prefix + line for line in value.splitlines()
            )
            return marker

        def template_host_values(value: Any, key: str | None = None) -> Any:
            if isinstance(value, dict):
                return {
                    item_key: template_host_values(item_value, str(item_key))
                    for item_key, item_value in value.items()
                }
            if isinstance(value, list):
                return [template_host_values(item, key) for item in value]
            # Compose commands intentionally retain $${VAR} for expansion by
            # the container shell. They must not become Helm expressions.
            if key in {"command", "args"}:
                return value
            if (
                isinstance(value, str)
                and value.startswith("{{")
                and value.endswith("}}")
            ):
                return raw_template(value)
            if isinstance(value, str) and self._contains_host_expression(value):
                expression = self._compose_expression(value)
                if key in {"port", "targetPort", "nodePort"}:
                    expression = f"{expression} | int"
                return template_value(expression, quote=(key == "host"))
            return value

        values = template_host_values(values)

        if service.param_source and service.param_filename:
            assert self.parameter_file is not None
            value_key = self.parameter_file["value_key"]
            filename = service.param_filename
            values[value_key] = template_value(
                f"readFile {self._go_string(filename)} | indent 4", quote=False
            )
            replacements[values[value_key]] = (
                f'|\n{{{{ readFile {self._go_string(filename)} | indent 4 }}}}'
            )

        for volume in values.get("volumes", []):
            filename = volume.pop("dataFile", None)
            if filename is None:
                continue
            marker = template_value(f'readFile {self._go_string(filename)}', quote=False)
            volume["data"] = marker
            replacements[marker] = (
                f'|\n{{{{ readFile {self._go_string(filename)} | indent 6 }}}}'
            )

        x11 = values.pop("x11", None)

        environment = values.setdefault("env", {})
        if x11:
            values["x11Display"] = template_value(
                'env "DISPLAY_X11_DOCKER" | default (env "DISPLAY")'
            )
            values["x11XauthorityFile"] = template_value(
                'env "XAUTHORITY_DOCKER"'
            )
        for key, expression in service.runtime_environment.items():
            environment[key] = template_value(
                self._compose_expression(expression)
            )
        if not environment:
            values.pop("env")

        for key, runtime_items in (
            ("command", service.runtime_command),
            ("args", service.runtime_args),
        ):
            if runtime_items is not None:
                values[key] = [
                    (
                        template_block(self._compose_expression(item), indent=4)
                        if "\n" in item
                        else template_value(self._compose_expression(item))
                    )
                    for item in runtime_items
                ]

        service_mapping = self.service_mappings.get(service.name, {})
        for key, rules in service_mapping.get("value_replacements", {}).items():
            if isinstance(values.get(key), list):
                values[key] = [
                    self._apply_replacements(item, rules) if isinstance(item, str) else item
                    for item in values[key]
                ]

        for key in ("command", "args"):
            items = values.get(key)
            if isinstance(items, list):
                values[key] = [
                    literal_block(item, indent=4)
                    if isinstance(item, str) and "\n" in item
                    else item
                    for item in items
                ]

        content = yaml.safe_dump(
            {self.values_root: values} if self.values_root else values,
            allow_unicode=True,
            sort_keys=False,
            width=1000,
        )
        root_key = f"{self.values_root}:\n" if self.values_root else ""
        if self.values_root and content == f"{self.values_root}: {{}}\n":
            content = root_key
        prefix_env = self.deployment.get("name_prefix_env")
        node_env = self.deployment.get("node_env")
        deployment_values = ""
        if prefix_env:
            deployment_values += f'  namePrefix: {{{{ env {self._go_string(prefix_env)} | quote }}}}\n'
        if node_env:
            deployment_values += (
                f'{{{{- if env {self._go_string(node_env)} }}}}\n'
                "  nodeSelector:\n"
                f'    kubernetes.io/hostname: {{{{ env {self._go_string(node_env)} | quote }}}}\n'
                "{{- end }}\n"
            )
        if root_key:
            content = content.replace(root_key, root_key + deployment_values, 1)
        elif deployment_values:
            root_values = "".join(
                line[2:] if line.startswith("  ") else line
                for line in deployment_values.splitlines(keepends=True)
            )
            content = root_values + content
        for marker, replacement in replacements.items():
            content = content.replace(marker, replacement)
        return content

    def _validate_inputs(self) -> None:
        for path, label in (
            (self.compose_file, "Compose file"),
            (self.env_file, "environment file"),
        ):
            if not path.is_file():
                raise ConversionError(f"{label} not found: {path}")

    @staticmethod
    def _print_summary(
        statuses: list[tuple[str, str, str, str]],
    ) -> None:
        for icon, service_name, target, message in statuses:
            suffix = f": {message}" if message else ""
            line = f"{icon} {service_name}{suffix}"
            if icon.strip().startswith("⚠"):
                line = f"\033[33m{line}\033[0m"
            elif icon.strip().startswith("❌"):
                line = f"\033[31m{line}\033[0m"
            print(line)

    def convert(self, dry_run: bool = False) -> None:
        self._validate_inputs()
        self.raw_services = self._discover_raw_services()
        # A value in a service's `environment:` block is container
        # configuration.  Only Compose interpolation (${...}) represents a
        # host-side input; a plain value such as PARAMS remains service-local.
        # By default export the full profile universe.  The optional focused
        # export uses the active Compose profiles instead.
        export_profiles = self.profiles if self.active_profiles_only else ["*"]
        full_config = self._resolve_compose(
            self.compose_file,
            profiles=export_profiles,
        )
        full_services = full_config["services"]
        if self.verbose:
            print("Compose services: " + ", ".join(sorted(full_services)))

        converted: list[ConvertedService] = []
        statuses: list[tuple[str, str, str, str]] = []
        fatal_errors: list[str] = []
        progress = tqdm(
            sorted(full_services),
            desc="Converting",
            unit="service",
        )
        for name in progress:
            raw_profiles = {
                str(profile)
                for profile in self.raw_services.get(name, {}).get("profiles", [])
            }
            if raw_profiles & self.excluded_profiles:
                continue
            display_name = (
                f"{name[:29]}..." if len(name) > 32 else name
            )
            progress.set_postfix_str(display_name.ljust(32))
            try:
                service = self._convert_service(name, full_services[name])
                converted.append(service)
                statuses.append(("✅", name, service.values_dir, ""))
            except ChartUnavailableError as exc:
                statuses.append(("❌", name, "failed", str(exc)))
                fatal_errors.append(str(exc))
            except ConversionError as exc:
                message = str(exc)
                statuses.append(("❌", name, "failed", message))
                fatal_errors.append(f"{name}: {message}")
        if fatal_errors:
            self._print_summary(statuses)
            raise ConversionError("; ".join(fatal_errors))
        if len({s.release_name for s in converted}) != len(converted):
            raise ConversionError("Compose service names produce duplicate Helm release names")
        converted.sort(
            key=lambda service: (
                service.values_dir,
                service.release_name,
            )
        )

        artifacts: dict[Path, str] = {
            Path("helmfile.yaml"): self._render_helmfile(converted),
            Path("environments/services.yaml.gotmpl"): self._render_services(converted),
        }
        for service in converted:
            artifacts[
                Path(service.values_dir) / "values.yaml.gotmpl"
            ] = self._render_values(service)
            if service.param_source and service.param_filename:
                param_data = service.param_source.read_text(encoding="utf-8")
                rules = self.service_mappings.get(service.name, {}).get(
                    "param_replacements", []
                )
                artifacts[Path(service.values_dir) / service.param_filename] = (
                    self._apply_replacements(param_data, rules)
                )
            for filename, source in service.config_sources.items():
                config_data = source.read_text(encoding="utf-8")
                rules = self.service_mappings.get(service.name, {}).get(
                    "config_replacements", {}
                ).get(filename, [])
                artifacts[Path(service.values_dir) / filename] = (
                    self._apply_replacements(config_data, rules)
                )

        if dry_run:
            self._print_summary(statuses)
            for relative_path, content in artifacts.items():
                print(f"--- {relative_path}")
                print(content, end="" if content.endswith("\n") else "\n")
            for service in converted:
                for filename, source in service.file_sources.items():
                    print(f"--- {Path(service.values_dir) / filename} (copy {source})")
                for dirname, source in service.directory_sources.items():
                    print(f"--- {Path(service.values_dir) / dirname}/ (copy {source}/)")
            return

        for relative_path, content in artifacts.items():
            destination = self.output_dir / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content, encoding="utf-8")
        for service in converted:
            for filename, source in service.file_sources.items():
                destination = self.output_dir / service.values_dir / filename
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
            for dirname, source in service.directory_sources.items():
                destination = self.output_dir / service.values_dir / dirname
                if destination.exists():
                    shutil.rmtree(destination)
                shutil.copytree(
                    source, destination,
                    ignore=shutil.ignore_patterns(".gitignore"),
                )
        self._print_summary(statuses)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Convert Compose services to Helmfile."
        )
    )
    parser.add_argument(
        "compose_file", type=Path, help="top-level Docker Compose file"
    )
    parser.add_argument(
        "--chart-map", type=Path, help="explicit service-to-OCI-chart mappings"
    )
    parser.add_argument(
        "--env-file", type=Path,
        help="Compose environment file (default: .env beside compose_file)",
    )
    parser.add_argument(
        "--profile",
        dest="profiles",
        action="append",
        default=[],
        help="default Compose profile (repeatable; overridden by COMPOSE_PROFILES)",
    )
    parser.add_argument(
        "--active-profiles-only",
        action="store_true",
        help="export only the active Compose profiles instead of every profile",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("./helm-output"),
        help="output directory (default: ./helm-output)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print output without writing files",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="print conversion details",
    )
    args = parser.parse_args()

    try:
        ComposeToHelmfileConverter(
            compose_file=args.compose_file,
            profiles=args.profiles,
            output_dir=args.output_dir,
            verbose=args.verbose,
            env_file=args.env_file,
            chart_map=args.chart_map,
            active_profiles_only=args.active_profiles_only,
        ).convert(dry_run=args.dry_run)
    except ConversionError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
