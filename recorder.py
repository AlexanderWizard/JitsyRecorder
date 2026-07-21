"""Headless Jitsi audio recorder built on Playwright.

Joins a Jitsi Meet room by URL (muted, camera off), captures the mixed
remote audio via an injected MediaRecorder, streams the encoded chunks back
to Python, and transcodes the result to MP3 with ffmpeg.
"""
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright

# When frozen by PyInstaller, bundled data lives in sys._MEIPASS while the
# exe itself sits in OUTPUT_DIR's parent (so recordings land next to the exe).
if getattr(sys, "frozen", False):
    BUNDLE_DIR = Path(sys._MEIPASS)               # type: ignore[attr-defined]
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BUNDLE_DIR = Path(__file__).resolve().parent
    BASE_DIR = BUNDLE_DIR

CAPTURE_JS = (BUNDLE_DIR / "capture.js").read_text(encoding="utf-8")
OUTPUT_DIR = BASE_DIR / "recordings"
OUTPUT_DIR.mkdir(exist_ok=True)

# A short silent WAV used as the fake microphone input (we join muted anyway).
SILENT_WAV = BASE_DIR / "silence.wav"


def _find_edge() -> str | None:
    """Locate msedge.exe (or a Chrome fallback) on this machine."""
    override = os.environ.get("RECORDER_BROWSER_PATH")
    if override and Path(override).exists():
        return override
    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    return shutil.which("msedge") or shutil.which("chrome")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _ensure_silence() -> None:
    if SILENT_WAV.exists():
        return
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono",
         "-t", "1", str(SILENT_WAV)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


class RecorderSession:
    """One recording session. Runs its own asyncio loop on a worker thread."""

    def __init__(self, url: str, display_name: str = "Recorder", sid: str = ""):
        self.id = sid
        self.url = url
        self.display_name = display_name
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        room = urlparse(url).path.strip("/").split("/")[-1] or "meeting"
        room = "".join(c for c in room if c.isalnum() or c in "-_")[:40] or "meeting"
        self.room = room
        self.webm_path = OUTPUT_DIR / f"{room}_{stamp}.webm"
        self.mp3_path = OUTPUT_DIR / f"{room}_{stamp}.mp3"

        self.state = "starting"          # starting|recording|stopping|done|error
        self.error: str | None = None
        self.started_at: dt.datetime | None = None
        self.stopped_at: dt.datetime | None = None

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stop_evt: asyncio.Event | None = None
        self._file = None
        self._file_lock = threading.Lock()
        self._bytes = 0
        self._level = 0  # last reported audio level, 0..100

    # ---- public API (called from the HTTP thread) --------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def request_stop(self) -> None:
        if self._loop and self._stop_evt and not self._stop_evt.is_set():
            self._loop.call_soon_threadsafe(self._stop_evt.set)

    def status(self) -> dict:
        dur = None
        if self.started_at:
            end = self.stopped_at or dt.datetime.now()
            dur = round((end - self.started_at).total_seconds(), 1)
        return {
            "id": self.id,
            "name": self.display_name,
            "room": self.room,
            "state": self.state,
            "error": self.error,
            "url": self.url,
            "duration_sec": dur,
            "bytes_captured": self._bytes,
            "level": self._level if self.state == "recording" else 0,
            "mp3": self.mp3_path.name if self.mp3_path.exists() else None,
        }

    # ---- worker thread -----------------------------------------------------
    def _run(self) -> None:
        try:
            asyncio.run(self._main())
        except Exception as exc:  # noqa: BLE001
            self.state = "error"
            self.error = str(exc)

    def _write_chunk(self, b64: str) -> None:
        data = base64.b64decode(b64)
        with self._file_lock:
            if self._file:
                self._file.write(data)
                self._bytes += len(data)

    async def _main(self) -> None:
        _ensure_silence()
        self._loop = asyncio.get_running_loop()
        self._stop_evt = asyncio.Event()
        self._file = open(self.webm_path, "wb")

        # We launch Edge ourselves and attach over CDP instead of letting
        # Playwright spawn it through --remote-debugging-pipe. The pipe
        # transport is unreliable inside a PyInstaller onefile (handles aren't
        # always inherited), which surfaced as "browser has been closed".
        # A self-managed process + TCP DevTools port sidesteps that entirely.
        edge = _find_edge()
        if not edge:
            raise RuntimeError(
                "Не найден Edge или Chrome. Установите Microsoft Edge, "
                "либо укажите путь в переменной RECORDER_BROWSER_PATH."
            )
        profile_dir = tempfile.mkdtemp(prefix="jitsi_rec_")
        port = _free_port()
        proc = subprocess.Popen(
            [
                edge,
                "--headless=new",
                f"--remote-debugging-port={port}",
                f"--user-data-dir={profile_dir}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-extensions",
                "--disable-sync",
                "--mute-audio",
                "--autoplay-policy=no-user-gesture-required",
                "--use-fake-ui-for-media-stream",
                "--use-fake-device-for-media-stream",
                f"--use-file-for-fake-audio-capture={SILENT_WAV}",
                "--disable-features=IsolateOrigins,site-per-process",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

        cdp_url = await self._wait_for_cdp(port, proc)

        async with async_playwright() as pw:
            browser = await pw.chromium.connect_over_cdp(cdp_url)
            try:
                context = browser.contexts[0] if browser.contexts else await browser.new_context()
                page = context.pages[0] if context.pages else await context.new_page()

                # Receives base64 chunks from the injected recorder.
                await page.expose_binding(
                    "__sendChunk",
                    lambda _src, b64: self._write_chunk(b64),
                )
                await page.expose_binding(
                    "__sendLevel",
                    lambda _src, lvl: setattr(self, "_level", int(lvl)),
                )

                join_url = self._build_url()
                await page.goto(join_url, wait_until="domcontentloaded", timeout=60_000)

                await self._prejoin(page)
                await page.evaluate(CAPTURE_JS)

                self.state = "recording"
                self.started_at = dt.datetime.now()

                await self._stop_evt.wait()  # blocks until request_stop()

                self.state = "stopping"
                try:
                    await page.evaluate("window.__stopRecording && window.__stopRecording()")
                    await page.wait_for_timeout(2500)  # let the last chunk flush
                except Exception:  # noqa: BLE001
                    pass
            finally:
                try:
                    await browser.close()
                except Exception:  # noqa: BLE001
                    pass

        # Terminate the Edge process and clean up its throwaway profile.
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            proc.kill()
        shutil.rmtree(profile_dir, ignore_errors=True)

        with self._file_lock:
            if self._file:
                self._file.close()
                self._file = None

        self.stopped_at = dt.datetime.now()
        self._transcode()
        self.state = "done"

    async def _wait_for_cdp(self, port: int, proc: subprocess.Popen, timeout: float = 30.0) -> str:
        """Poll the DevTools endpoint until Edge is ready to accept CDP."""
        url = f"http://127.0.0.1:{port}/json/version"
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(
                    f"Браузер завершился при запуске (код {proc.returncode}). "
                    "Вероятно, его блокирует антивирус/политика безопасности."
                )
            try:
                data = await asyncio.to_thread(
                    lambda: urllib.request.urlopen(url, timeout=2).read()
                )
                if data:
                    return f"http://127.0.0.1:{port}"
            except Exception:  # noqa: BLE001
                await asyncio.sleep(0.5)
        raise RuntimeError("Браузер не открыл отладочный порт вовремя (таймаут).")

    def _build_url(self) -> str:
        # Pre-fill display name and disable prejoin where the deep-link
        # config params are honoured (meet.jit.si supports these).
        sep = "&" if "#" in self.url else "#"
        extra = (
            f'{sep}userInfo.displayName="{self.display_name}"'
            "&config.prejoinPageEnabled=false"
            "&config.startWithAudioMuted=true"
            "&config.startWithVideoMuted=true"
        )
        return self.url + extra

    async def _prejoin(self, page) -> None:
        """Best-effort click through the prejoin screen if it appears."""
        selectors = [
            'div[data-testid="prejoin.joinMeeting"]',
            'div.action-btn',
            'button[aria-label*="Join"]',
            'text=Join meeting',
        ]
        for _ in range(20):
            for sel in selectors:
                try:
                    el = await page.query_selector(sel)
                    if el:
                        await el.click(timeout=1000)
                        await page.wait_for_timeout(1000)
                        return
                except Exception:  # noqa: BLE001
                    continue
            # No prejoin visible — assume we're already in the room.
            try:
                if await page.query_selector('.videocontainer, #largeVideoContainer'):
                    return
            except Exception:  # noqa: BLE001
                pass
            await page.wait_for_timeout(1000)

    def _transcode(self) -> None:
        if not self.webm_path.exists() or self.webm_path.stat().st_size == 0:
            self.state = "error"
            self.error = "No audio captured (empty stream)."
            return
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(self.webm_path),
             "-vn", "-codec:a", "libmp3lame", "-b:a", "128k",
             str(self.mp3_path)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            os.remove(self.webm_path)
        except OSError:
            pass
