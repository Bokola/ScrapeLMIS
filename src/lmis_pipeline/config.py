"""Configuration loader: reads config.yaml, resolves credentials from
environment variables (never from the file itself), and exposes the
.get() / .selectors() interface every other module in this package expects.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import find_dotenv, load_dotenv

from .logger import get_logger

log = get_logger(__name__)


class ConfigError(RuntimeError):
    pass


class Config:
    """Thin wrapper around the parsed config.yaml dict plus resolved
    credentials. Construct via Config.load(path) in normal use - the plain
    constructor is mainly useful for tests that want to pass data={...}
    directly without a file on disk.
    """

    def __init__(
        self,
        data: dict,
        root: str | Path = "./run_data",
        headless: bool = True,
        username: str | None = None,
        password: str | None = None,
    ):
        self.data = data
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.headless = headless
        self.username = username
        self.password = password

    @classmethod
    def load(cls, path: str | Path = "config.yaml", env_file: str | Path | None = None) -> "Config":
        """Load config.yaml and resolve credentials from the environment
        variables named in credentials.username_env / credentials.password_env
        (defaults: LMIS_username / LMIS_pass). Raises ConfigError if the
        file is missing or either variable isn't set - fails loudly at
        startup rather than partway through a run.

        Loads a .env file BEFORE reading os.environ, so credentials work
        with plain `python -m lmis_pipeline.main` / `uv run python -m ...`
        without needing to remember `uv run --env-file .env ...`. Real
        already-exported shell/CI env vars always win (override=False) -
        .env only fills in what isn't already set. env_file lets you point
        at a specific file (e.g. .env.production); left as None, it walks up
        from the current directory looking for the nearest .env, same as
        every other dotenv-based tool.
        """
        dotenv_path = find_dotenv(str(env_file)) if env_file else find_dotenv()
        if dotenv_path:
            load_dotenv(dotenv_path, override=False)
            log.info("Loaded environment variables from %s", dotenv_path)
        else:
            log.debug("No .env file found - relying on already-exported environment variables")

        path = Path(path)
        if not path.exists():
            raise ConfigError(f"Config file not found: {path}")

        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        creds = data.get("credentials", {}) or {}
        username_env = creds.get("username_env", "LMIS_username")
        password_env = creds.get("password_env", "LMIS_pass")
        username = os.environ.get(username_env)
        password = os.environ.get(password_env)

        missing = [
            name for name, val in [(username_env, username), (password_env, password)]
            if not val
        ]
        if missing:
            raise ConfigError(
                f"Missing required environment variable(s): {', '.join(missing)}. "
                "Set them in your shell, or add them to a .env file in the "
                "project directory (see .env.example) - it's loaded automatically."
            )

        log.info("Loaded config from %s", path)
        return cls(
            data=data,
            root=data.get("root", "./run_data"),
            headless=data.get("headless", True),
            username=username,
            password=password,
        )

    def get(self, dotted_key: str, default: Any = None) -> Any:
        """Read a dotted-path key, e.g. cfg.get('timeouts.action_ms', 30000)."""
        node: Any = self.data
        for part in dotted_key.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def selectors(self, dotted_key: str) -> list[str]:
        """Read a selector list, e.g. cfg.selectors('login.username_input').

        Every call site in this codebase passes a path relative to the
        top-level 'selectors:' section of config.yaml (e.g. 'login.username_input'
        means selectors.login.username_input) - so that prefix is added
        here, once, rather than at every call site.

        Always returns a list (empty if missing), since every caller
        iterates over it directly without checking the type first."""
        value = self.get(f"selectors.{dotted_key}", [])
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        if not isinstance(value, list):
            log.warning(
                "Expected a list for selector key 'selectors.%s', got %s - "
                "treating as empty",
                dotted_key, type(value).__name__,
            )
            return []
        return value
