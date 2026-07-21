"""Persistent settings + Windows autostart helpers.

Settings live in config.json next to the exe. Environment variables still win
over the file so existing launch scripts keep working.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


CONFIG_PATH = _base_dir() / "config.json"

DEFAULTS: dict = {
    "host": "0.0.0.0",
    "port": 9999,
    "token": "",
    "whisper_model": "large-v3",
    "whisper_language": "ru",
    "whisper_device": "cuda",
    "whisper_compute": "float16",
}

# Map config keys to the env vars that override them (if set).
_ENV = {
    "host": "RECORDER_HOST",
    "port": "RECORDER_PORT",
    "token": "RECORDER_TOKEN",
    "whisper_model": "WHISPER_MODEL",
    "whisper_language": "WHISPER_LANGUAGE",
    "whisper_device": "WHISPER_DEVICE",
    "whisper_compute": "WHISPER_COMPUTE",
}


def load() -> dict:
    cfg = dict(DEFAULTS)
    try:
        if CONFIG_PATH.exists():
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except Exception:  # noqa: BLE001
        pass
    # env overrides
    for key, env in _ENV.items():
        val = os.environ.get(env)
        if val not in (None, ""):
            cfg[key] = int(val) if key == "port" else val
    try:
        cfg["port"] = int(cfg["port"])
    except (ValueError, TypeError):
        cfg["port"] = DEFAULTS["port"]
    return cfg


def save(cfg: dict) -> None:
    data = {k: cfg.get(k, DEFAULTS[k]) for k in DEFAULTS}
    try:
        data["port"] = int(data["port"])
    except (ValueError, TypeError):
        data["port"] = DEFAULTS["port"]
    CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# --- Windows autostart (HKCU Run key) -------------------------------------
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_APP_NAME = "JitsiRecorder"


def _exe_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{Path(sys.executable).resolve()}"'
    # dev mode: run tray.py with the current interpreter
    return f'"{Path(sys.executable).resolve()}" "{_base_dir() / "tray.py"}"'


def autostart_enabled() -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as k:
            winreg.QueryValueEx(k, _APP_NAME)
        return True
    except OSError:
        return False


def set_autostart(enable: bool) -> None:
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0,
                        winreg.KEY_SET_VALUE) as k:
        if enable:
            winreg.SetValueEx(k, _APP_NAME, 0, winreg.REG_SZ, _exe_command())
        else:
            try:
                winreg.DeleteValue(k, _APP_NAME)
            except OSError:
                pass
