"""
Configuration management service.

Handles loading, saving, and parsing of user-defined configuration files,
such as the manual chassis MAC-to-IP mapping (manual_chassis_map.json).

Kept free of any PySide6/Qt imports so it can be tested and used without
a GUI.
"""

import json
import os
import re
import tempfile
from pathlib import Path

# Default chassis mapping baked into the app, mirroring MANUAL_CHASSIS_MAP
# hardcoded in the original Topology.py. These always apply so a known
# switch resolves even when the user provides no manual map at all.
DEFAULT_MANUAL_CHASSIS_MAP = {
    "00:17:7c:6b:2d:2a": "192.168.1.22",
}


def effective_manual_chassis_map(raw_text: str = "") -> dict[str, str]:
    """
    Manual chassis map actually used for resolution: the built-in defaults
    merged with any user-specified entries (user entries win on conflict).
    Mirrors the reference project, where MANUAL_CHASSIS_MAP was hardcoded.
    """
    merged = dict(DEFAULT_MANUAL_CHASSIS_MAP)
    merged.update(parse_manual_chassis_map(raw_text))
    return merged


def manual_chassis_map_path() -> Path:
    """Project root path: <project>/manual_chassis_map.json."""
    return Path(__file__).resolve().parent.parent.parent / "manual_chassis_map.json"


def normalize_mac(mac: str) -> str:
    """Normalize MAC address to lowercase, colon-separated format."""
    return mac.strip().lower().replace("-", ":")


def load_manual_chassis_map(path: Path | str | None = None) -> dict[str, str]:
    """
    Reads the manual chassis mapping from JSON.
    Returns a dict mapping normalized MAC -> IP string.
    Returns {} if the file does not exist or cannot be parsed.
    """
    target = Path(path) if path else manual_chassis_map_path()
    if not target.exists():
        return {}

    try:
        with open(target, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return {
            normalize_mac(str(k)): str(v).strip()
            for k, v in data.items()
            if str(k).strip() and str(v).strip()
        }
    except Exception:
        return {}


def save_manual_chassis_map(mapping: dict[str, str], path: Path | str | None = None) -> None:
    """
    Writes mapping dict to JSON atomically.
    Ensures the parent directory exists and replaces the destination file safely.
    """
    target = Path(path) if path else manual_chassis_map_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    normalized_data = {
        normalize_mac(str(k)): str(v).strip()
        for k, v in (mapping or {}).items()
        if str(k).strip() and str(v).strip()
    }

    # Write to temp file in same directory for atomic replace
    temp_file = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            delete=False,
            suffix=".tmp",
        ) as f:
            json.dump(normalized_data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
            temp_file = Path(f.name)

        os.replace(temp_file, target)
    except Exception:
        if temp_file and temp_file.exists():
            try:
                temp_file.unlink()
            except OSError:
                pass
        raise


def parse_manual_chassis_map(text: str) -> dict[str, str]:
    """
    Parses a user input string into a MAC -> IP dict.
    Supports comma, semicolon, or newline separated key=value pairs.
    Example: '00:17:7c:6b:2d:2a=192.168.1.22, 00-11-22-33-44-55=192.168.1.50'
    """
    if not text:
        return {}

    result = {}
    # Split by comma, semicolon, or newline
    entries = re.split(r"[,;\n]+", text)
    for entry in entries:
        entry = entry.strip()
        if not entry:
            continue
        if "=" in entry:
            mac_part, ip_part = entry.split("=", 1)
        elif "->" in entry:
            mac_part, ip_part = entry.split("->", 1)
        elif " " in entry:
            parts = entry.split(None, 1)
            mac_part, ip_part = parts[0], parts[1]
        else:
            continue

        mac = normalize_mac(mac_part)
        ip = ip_part.strip()
        if mac and ip:
            result[mac] = ip

    return result


def format_manual_chassis_map(mapping: dict[str, str]) -> str:
    """
    Formats a MAC -> IP mapping dict into a comma-separated key=value string
    for display in the GUI input field.
    """
    if not mapping:
        return ""
    return ", ".join(f"{mac}={ip}" for mac, ip in mapping.items())
