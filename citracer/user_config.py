"""Persistent user-level config for citracer.

Stores small key-value settings (API keys, preferences) in a JSON file
under the user's home directory:

    ~/.citracer/config.json    (Linux / macOS / Windows alike)

The Semantic Scholar API key is the only setting we care about right now,
but the loader/saver are generic so future settings can join without
rewiring everything.

Why a dedicated user config rather than reusing the project ``.env``:
``.env`` lives in a project directory and only loads when ``citracer`` is
run from there. A user-global config is what people expect when they say
"I set my key once and forget about it".
"""
from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path

logger = logging.getLogger(__name__)

CONFIG_DIR_NAME = ".citracer"
CONFIG_FILE_NAME = "config.json"


def config_dir() -> Path:
    """Return the user-level config directory (creating it if needed)."""
    p = Path.home() / CONFIG_DIR_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def config_file() -> Path:
    """Return the path to the user config file (may not yet exist)."""
    return config_dir() / CONFIG_FILE_NAME


def load_config() -> dict:
    """Read the config file and return its contents as a dict.

    Returns an empty dict if the file doesn't exist or is corrupt.
    """
    path = config_file()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Could not read %s: %s — treating as empty", path, e)
        return {}


def save_config(data: dict) -> Path:
    """Write the config file atomically and tighten its permissions.

    On POSIX systems we chmod 600 so other local users can't read your
    API key. On Windows the chmod is a no-op (the OS doesn't honour
    POSIX bits) but the user-home location already provides reasonable
    isolation.
    """
    path = config_file()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0o600
    except OSError:
        pass
    return path


def get_s2_api_key() -> str | None:
    """Return the Semantic Scholar API key from the user config, if any."""
    return load_config().get("s2_api_key")


def set_s2_api_key(key: str) -> Path:
    """Persist the Semantic Scholar API key. Returns the config file path."""
    data = load_config()
    data["s2_api_key"] = key
    return save_config(data)


def get_email() -> str | None:
    """Return the OpenAlex email from the user config, if any."""
    return load_config().get("email")


def set_email(email: str) -> Path:
    """Persist the OpenAlex email. Returns the config file path."""
    data = load_config()
    data["email"] = email
    return save_config(data)


def clear_email() -> bool:
    """Remove the email from the user config."""
    data = load_config()
    if "email" not in data:
        return False
    del data["email"]
    save_config(data)
    return True


def clear_s2_api_key() -> bool:
    """Remove the Semantic Scholar API key from the user config.

    Returns True iff a key was present and got removed.
    """
    data = load_config()
    if "s2_api_key" not in data:
        return False
    del data["s2_api_key"]
    save_config(data)
    return True


def mask_secret(secret: str | None, *, head: int = 4, tail: int = 4) -> str:
    """Return a redacted form of a secret for safe display.

    ``"pypi-AgEIcHlwaS5vcmc..."`` -> ``"pypi***...orc..."``
    """
    if not secret:
        return "(unset)"
    if len(secret) <= head + tail:
        return "*" * len(secret)
    return f"{secret[:head]}{'*' * (len(secret) - head - tail)}{secret[-tail:]}"
