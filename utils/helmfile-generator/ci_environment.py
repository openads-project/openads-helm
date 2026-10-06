#!/usr/bin/env python3
"""Export caller-owned dotenv values needed for Helmfile validation."""

from __future__ import annotations

import argparse
import re
import secrets
from pathlib import Path

import yaml
from dotenv import dotenv_values


ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
BLOCKED_ENV_NAMES = {
    "BASH_ENV",
    "ENV",
    "LD_PRELOAD",
    "NODE_OPTIONS",
    "PATH",
    "PYTHONPATH",
    "SHELLOPTS",
}


def _write_github_env(destination: Path, values: dict[str, str]) -> None:
    with destination.open("a", encoding="utf-8") as output:
        for name, value in values.items():
            if not ENV_NAME.fullmatch(name):
                raise ValueError(f"invalid environment variable name: {name}")
            if (
                name in BLOCKED_ENV_NAMES
                or name.startswith("ACTIONS_")
                or name.startswith("GITHUB_")
                or name.startswith("RUNNER_")
            ):
                raise ValueError(f"refusing to override CI environment variable: {name}")
            delimiter = f"helmfile_generator_{secrets.token_hex(12)}"
            if delimiter in value:
                raise ValueError(f"cannot safely export environment variable: {name}")
            output.write(f"{name}<<{delimiter}\n{value}\n{delimiter}\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--compose-file", type=Path, required=True)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--chart-map", type=Path)
    parser.add_argument("--profiles", default="")
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--github-env", type=Path, required=True)
    args = parser.parse_args()

    compose_file = args.compose_file.resolve()
    env_file = args.env_file.resolve() if args.env_file else compose_file.parent / ".env"
    if not env_file.is_file():
        raise FileNotFoundError(f"environment file not found: {env_file}")
    values: dict[str, str] = {}
    for name, value in dotenv_values(env_file).items():
        if value is not None:
            values[name] = value

    if args.profiles:
        values["COMPOSE_PROFILES"] = args.profiles

    if args.chart_map:
        chart_map = yaml.safe_load(args.chart_map.read_text(encoding="utf-8")) or {}
        deployment = chart_map.get("deployment", {})
        name_prefix_env = deployment.get("name_prefix_env")
        if name_prefix_env:
            values.setdefault(name_prefix_env, "ci")
        host_path_root_env = deployment.get("host_path_root_env")
        if host_path_root_env:
            values.setdefault(host_path_root_env, str(args.workspace.resolve()))

    _write_github_env(args.github_env, values)


if __name__ == "__main__":
    main()
