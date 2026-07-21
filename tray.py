"""System-tray entry point for Jitsi Recorder.

Runs the FastAPI/uvicorn server in a background thread and shows a tray icon
with a small menu instead of a console window. Build this file with
PyInstaller --windowed to get a console-less tray app.
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import traceback
import webbrowser
from pathlib import Path

# In --windowed builds sys.stdout/stderr are None; redirect them to a log file
# so library logging (uvicorn) doesn't crash and errors are recoverable.
if getattr(sys, "frozen", False):
    _log_dir = Path(sys.executable).resolve().parent
else:
    _log_dir = Path(__file__).resolve().parent
LOG_PATH = _log_dir / "recorder.log"
if sys.stdout is None or sys.stderr is None:
    _logf = open(LOG_PATH, "a", buffering=1, encoding="utf-8")
    sys.stdout = _logf
    sys.stderr = _logf

import uvicorn
from PIL import Image, ImageDraw
import pystray

import config
from server import app
from version import AUTHOR, VERSION

_CFG = config.load()
HOST = _CFG["host"]
PORT = _CFG["port"]


def _lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:  # noqa: BLE001
        return "127.0.0.1"


PANEL_URL = f"http://{_lan_ip()}:{PORT}"
LOCAL_URL = f"http://127.0.0.1:{PORT}"


def _make_icon() -> Image.Image:
    """A simple red 'record' dot on a dark rounded square."""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([2, 2, size - 2, size - 2], radius=14, fill=(26, 33, 48, 255))
    d.ellipse([18, 18, size - 18, size - 18], fill=(239, 68, 68, 255))
    return img


def _run_server() -> None:
    try:
        uvicorn.run(app, host=HOST, port=PORT, access_log=False, log_level="warning")
    except Exception:  # noqa: BLE001
        traceback.print_exc()


def _open_settings() -> None:
    """Build a small tkinter settings window in its own thread."""
    import tkinter as tk
    from tkinter import messagebox, ttk

    cfg = config.load()
    root = tk.Tk()
    root.title("Jitsi Recorder — Настройки")
    root.resizable(False, False)
    try:
        root.attributes("-topmost", True)
    except Exception:  # noqa: BLE001
        pass

    pad = {"padx": 10, "pady": 4}
    row = 0

    def field(label, value, show=None):
        nonlocal row
        ttk.Label(root, text=label).grid(row=row, column=0, sticky="w", **pad)
        var = tk.StringVar(value=str(value))
        ttk.Entry(root, textvariable=var, width=34, show=show).grid(
            row=row, column=1, sticky="w", **pad)
        row += 1
        return var

    v_host = field("Хост (0.0.0.0 = вся сеть)", cfg["host"])
    v_port = field("Порт", cfg["port"])
    v_token = field("Токен (пусто = без защиты)", cfg["token"], show="•")
    v_model = field("whisper: модель", cfg["whisper_model"])
    v_lang = field("whisper: язык", cfg["whisper_language"])
    v_device = field("whisper: устройство", cfg["whisper_device"])
    v_compute = field("whisper: compute_type", cfg["whisper_compute"])

    v_auto = tk.BooleanVar(value=config.autostart_enabled())
    ttk.Checkbutton(root, text="Автозапуск при входе в Windows",
                    variable=v_auto).grid(row=row, column=0, columnspan=2,
                                          sticky="w", **pad)
    row += 1

    def on_save():
        new = {
            "host": v_host.get().strip() or "0.0.0.0",
            "port": v_port.get().strip() or "9999",
            "token": v_token.get(),
            "whisper_model": v_model.get().strip(),
            "whisper_language": v_lang.get().strip(),
            "whisper_device": v_device.get().strip(),
            "whisper_compute": v_compute.get().strip(),
        }
        try:
            config.save(new)
            config.set_autostart(v_auto.get())
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Ошибка", str(exc))
            return
        messagebox.showinfo(
            "Сохранено",
            "Настройки сохранены.\nИзменения хоста/порта/токена вступят в силу "
            "после перезапуска приложения.")
        root.destroy()

    btns = ttk.Frame(root)
    btns.grid(row=row, column=0, columnspan=2, pady=10)
    ttk.Button(btns, text="Сохранить", command=on_save).pack(side="left", padx=6)
    ttk.Button(btns, text="Отмена", command=root.destroy).pack(side="left", padx=6)

    root.mainloop()


def main() -> None:
    threading.Thread(target=_run_server, daemon=True).start()

    def open_panel(icon, item):  # noqa: ARG001
        webbrowser.open(LOCAL_URL)

    def _settings_safe():
        try:
            _open_settings()
        except Exception:  # noqa: BLE001
            traceback.print_exc()

    def open_settings(icon, item):  # noqa: ARG001
        threading.Thread(target=_settings_safe, daemon=True).start()

    def toggle_autostart(icon, item):  # noqa: ARG001
        config.set_autostart(not config.autostart_enabled())
        icon.update_menu()

    def quit_app(icon, item):  # noqa: ARG001
        icon.stop()
        os._exit(0)

    menu = pystray.Menu(
        pystray.MenuItem(f"Jitsi Recorder v{VERSION}", None, enabled=False),
        pystray.MenuItem(PANEL_URL, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Открыть панель", open_panel, default=True),
        pystray.MenuItem("Настройки…", open_settings),
        pystray.MenuItem("Автозапуск", toggle_autostart,
                         checked=lambda item: config.autostart_enabled()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Выход", quit_app),
    )
    icon = pystray.Icon(
        "jitsi_recorder", _make_icon(),
        f"Jitsi Recorder v{VERSION} — {PANEL_URL}", menu,
    )
    icon.run()


if __name__ == "__main__":
    main()
