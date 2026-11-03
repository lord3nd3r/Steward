"""XDG config and data locations, with environment overrides for tests."""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "steward"


def config_dir() -> Path:
    override = os.environ.get("STEWARD_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / APP_NAME


def data_dir() -> Path:
    override = os.environ.get("STEWARD_DATA_DIR")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return base / APP_NAME


def config_path() -> Path:
    return config_dir() / "config.toml"


def database_path() -> Path:
    return data_dir() / "steward.db"
