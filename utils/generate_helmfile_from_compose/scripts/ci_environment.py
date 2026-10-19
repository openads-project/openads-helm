#!/usr/bin/env python3
"""Export caller-owned dotenv values needed for Helmfile validation."""

from __future__ import annotations

import argparse
import re
import secrets
import shlex
from pathlib import Path

from dotenv import dotenv_values

from generate_helmfile_from_compose import HOST_PATH_ROOT_ENV


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
BLOCKED_ENV_PREFIXES = ("ACTIONS_", "GITHUB_", "RUNNER_", "CI_", "GITLAB_")


def _validate_names(values: dict[str, str]) -> None:
    for name in values:
        if not ENV_NAME.fullmatch(name):
            raise ValueError(f"invalid environment variable name: {name}")
        if name in BLOCKED_ENV_NAMES or name.startswith(BLOCKED_ENV_PREFIXES):
            raise ValueError(f"refusing to override CI environment variable: {name}")


def _write_github_env(destination: Path, values: dict[str, str]) -> None:
    _validate_names(values)
    with destination.open("a", encoding="utf-8") as output:
        for name, value in values.items():
            delimiter = f"generate_helmfile_from_compose_{secrets.token_hex(12)}"
            if delimiter in value:
                raise ValueError(f"cannot safely export environment variable: {name}")
            output.write(f"{name}<<{delimiter}\n{value}\n{delimiter}\n")


def _write_shell_env(destination: Path, values: dict[str, str]) -> None:
    _validate_names(values)
    with destination.open("a", encoding="utf-8") as output:
        for name, value in values.items():
            output.write(f"export {name}={shlex.quote(value)}\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--compose-file", type=Path, required=True)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--profiles", default="")
    parser.add_argument("--workspace", type=Path, required=True)
    destination = parser.add_mutually_exclusive_group(required=True)
    destination.add_argument("--github-env", type=Path)
    destination.add_argument("--shell-env", type=Path)
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

    values.setdefault(HOST_PATH_ROOT_ENV, str(args.workspace.resolve()))

    if args.github_env:
        _write_github_env(args.github_env, values)
    else:
        _write_shell_env(args.shell_env, values)


if __name__ == "__main__":
    main()
