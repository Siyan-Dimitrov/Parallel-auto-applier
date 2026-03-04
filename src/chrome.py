"""Chrome CDP (Chrome DevTools Protocol) management.

Launches Chrome with a remote-debugging-port so that Playwright MCP (or any
CDP client) can connect via ``http://localhost:{port}`` and reuse the same
persistent browser session -- cookies, login state, extensions, etc.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from src.utils.logging import get_logger

logger = get_logger()

# ---------------------------------------------------------------------------
# Chrome path detection (Windows)
# ---------------------------------------------------------------------------

_CANDIDATE_PATHS: list[str] = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


def get_chrome_path() -> str | None:
    """Return the path to a Chrome/Chromium binary, or *None* if not found.

    Checks well-known installation directories and ``%LOCALAPPDATA%``.
    """
    # Explicit candidates first
    for candidate in _CANDIDATE_PATHS:
        if os.path.isfile(candidate):
            return candidate

    # %LOCALAPPDATA%\Google\Chrome\Application\chrome.exe
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        local_chrome = os.path.join(
            local_app_data, "Google", "Chrome", "Application", "chrome.exe"
        )
        if os.path.isfile(local_chrome):
            return local_chrome

    # Quick registry check (HKLM, CurrentVersion\App Paths)
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe",
        )
        value, _ = winreg.QueryValueEx(key, "")
        winreg.CloseKey(key)
        if value and os.path.isfile(value):
            return value
    except Exception:
        pass

    return None


# ---------------------------------------------------------------------------
# Port helpers
# ---------------------------------------------------------------------------


def ensure_port_free(port: int) -> None:
    """Kill whatever process is listening on *port* (Windows-only, best-effort)."""
    try:
        result = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        for line in result.stdout.splitlines():
            # Look for LISTENING lines that match the port
            if f":{port}" in line and "LISTENING" in line:
                parts = line.split()
                pid = parts[-1]
                if pid.isdigit() and int(pid) > 0:
                    logger.debug("Killing PID %s occupying port %d", pid, port)
                    subprocess.run(
                        ["taskkill", "/F", "/PID", pid],
                        capture_output=True,
                        timeout=10,
                    )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Chrome launch / kill
# ---------------------------------------------------------------------------


def launch_chrome(
    port: int = 9222,
    profile_dir: str = "data/chrome_profiles/worker_0",
    chrome_path: str | None = None,
) -> subprocess.Popen:
    """Launch Chrome with remote debugging enabled and return the *Popen* handle.

    Parameters
    ----------
    port:
        Remote-debugging port for CDP.
    profile_dir:
        User-data directory (relative paths are resolved from cwd).
    chrome_path:
        Explicit path to ``chrome.exe``.  Auto-detected when *None*.
    """
    if chrome_path is None:
        chrome_path = get_chrome_path()
    if chrome_path is None:
        raise FileNotFoundError(
            "Could not find Chrome. Pass chrome_path explicitly or install Chrome."
        )

    profile = Path(profile_dir).resolve()
    profile.mkdir(parents=True, exist_ok=True)

    # Suppress the "Chrome didn't shut down correctly" restore nag by
    # pre-writing a clean Preferences file.
    prefs_dir = profile / "Default"
    prefs_dir.mkdir(parents=True, exist_ok=True)
    prefs_file = prefs_dir / "Preferences"
    prefs: dict = {}
    if prefs_file.exists():
        try:
            prefs = json.loads(prefs_file.read_text(encoding="utf-8"))
        except Exception:
            prefs = {}
    prefs.setdefault("profile", {})
    prefs["profile"]["exit_type"] = "Normal"
    prefs["profile"]["exited_cleanly"] = True
    prefs_file.write_text(json.dumps(prefs, indent=2), encoding="utf-8")

    args = [
        chrome_path,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate",
        "--disable-background-networking",
        "--disable-sync",
        "--disable-hang-monitor",
        "about:blank",
    ]

    logger.info("Launching Chrome on CDP port %d  (profile: %s)", port, profile)
    logger.debug("Chrome command: %s", " ".join(args))

    proc = subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    logger.info("Chrome started  (PID %d)", proc.pid)
    return proc


def kill_chrome(port: int = 9222) -> None:
    """Kill the Chrome process tree listening on *port* (best-effort)."""
    try:
        result = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        for line in result.stdout.splitlines():
            if f":{port}" in line and "LISTENING" in line:
                parts = line.split()
                pid = parts[-1]
                if pid.isdigit() and int(pid) > 0:
                    logger.info("Killing Chrome process tree (PID %s, port %d)", pid, port)
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", pid],
                        capture_output=True,
                        timeout=10,
                    )
                    return
        logger.debug("No process found listening on port %d", port)
    except Exception as exc:
        logger.debug("kill_chrome failed: %s", exc)


# ---------------------------------------------------------------------------
# CDP readiness check
# ---------------------------------------------------------------------------


def wait_for_cdp(port: int = 9222, timeout: float = 15.0) -> bool:
    """Block until the CDP endpoint on *port* responds, or *timeout* elapses.

    Returns ``True`` when Chrome's ``/json/version`` is reachable,
    ``False`` on timeout.
    """
    url = f"http://localhost:{port}/json/version"
    deadline = time.monotonic() + timeout
    interval = 0.25

    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read())
                    logger.info(
                        "CDP ready on port %d  (%s)",
                        port,
                        data.get("Browser", "unknown"),
                    )
                    return True
        except (URLError, OSError, ValueError):
            pass
        time.sleep(interval)

    logger.warning("CDP on port %d did not become ready within %.1fs", port, timeout)
    return False
