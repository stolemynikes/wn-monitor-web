#!/usr/bin/env python3
"""Process control for the radar — start/stop/status on macOS, Linux, Windows.

Replaces the macOS-only shell script (bash + tmux + caffeinate) so the web panel
has one cross-platform way to manage the monitor process. Usable directly:

    python control.py start | stop | restart | status
"""

import json
import os
import platform
import signal
import subprocess
import sys
import time
from pathlib import Path

import psutil

PROJECT_DIR = Path(__file__).resolve().parent
MONITOR = PROJECT_DIR / "monitor.py"
PID_FILE = PROJECT_DIR / ".radar.pid"
LOG_FILE = PROJECT_DIR / "radar.log"
IS_WINDOWS = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"

STOP_GRACE_SECONDS = 15  # monitor closes tabs and flushes state on shutdown


def _python() -> str:
    """The interpreter running us — works inside a venv without activation."""
    return sys.executable


def read_pid():
    """PID of our running monitor, or None. Cleans up a stale PID file."""
    try:
        pid = int(PID_FILE.read_text().strip())
    except (OSError, ValueError):
        return None
    try:
        proc = psutil.Process(pid)
        # Guard against PID reuse: it must actually be our monitor.
        if "monitor.py" in " ".join(proc.cmdline()):
            return pid
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    PID_FILE.unlink(missing_ok=True)
    return None


def foreign_instances():
    """Other monitor.py processes outside this project.

    Two radars driving separate browsers from one IP doubles the traffic
    signature — which is what tripped Cloudflare before. Refuse to add to it.
    """
    found = []
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            argv = proc.info["cmdline"] or []
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        # Identify by the SCRIPT path, not the whole command line: our venv's
        # interpreter lives under PROJECT_DIR, so a substring test on the full
        # cmdline wrongly claims any process launched with it as ours.
        script = next((a for a in argv if a.endswith("monitor.py")), None)
        if script is None or "run" not in argv:
            continue
        try:
            is_ours = Path(script).resolve() == MONITOR
        except OSError:
            is_ours = False
        if not is_ours:
            found.append((proc.info["pid"], " ".join(argv)))
    return found


def start(force: bool = False) -> str:
    if (pid := read_pid()):
        return f"already running (pid {pid})"
    if not force and (others := foreign_instances()):
        listed = "; ".join(f"pid {p}" for p, _ in others)
        return ("refusing to start: another radar is running outside this "
                f"project ({listed}). Two instances double the traffic to "
                "Whatnot from one IP. Stop the other one first, or use "
                "--force if you're sure.")
    if not (PROJECT_DIR / "config.json").exists():
        return "config.json missing — copy config.example.json and fill it in"

    # Stale browser locks left by a hard kill stop Chromium starting.
    for lock in PROJECT_DIR.glob("whatnot-profile/Singleton*"):
        lock.unlink(missing_ok=True)

    cmd = [_python(), str(MONITOR), "run"]
    if IS_MAC:
        # Keep macOS from idle-sleeping or App-Napping the browser, which
        # silently freezes detection.
        cmd = ["caffeinate", "-dis"] + cmd

    kwargs = {}
    if IS_WINDOWS:
        # New process group so we can send CTRL_BREAK for a graceful stop.
        kwargs["creationflags"] = (subprocess.CREATE_NEW_PROCESS_GROUP
                                   | subprocess.DETACHED_PROCESS)
    else:
        kwargs["start_new_session"] = True  # survive the parent shell closing

    with open(LOG_FILE, "a", buffering=1) as log:
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT,
                                cwd=str(PROJECT_DIR), **kwargs)
    PID_FILE.write_text(str(proc.pid))

    time.sleep(3)
    if read_pid() is None:
        return f"failed to start — see {LOG_FILE.name}"
    return f"started (pid {proc.pid})"


def stop() -> str:
    pid = read_pid()
    if pid is None:
        return "not running"
    proc = psutil.Process(pid)
    # Graceful first: the monitor closes tabs and flushes state on SIGTERM.
    try:
        if IS_WINDOWS:
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            proc.send_signal(signal.SIGTERM)
    except (psutil.NoSuchProcess, OSError):
        pass
    try:
        proc.wait(timeout=STOP_GRACE_SECONDS)
        outcome = "stopped"
    except psutil.TimeoutExpired:
        for child in proc.children(recursive=True):
            child.kill()
        proc.kill()
        outcome = "force-killed (did not exit gracefully)"
    PID_FILE.unlink(missing_ok=True)
    return outcome


def restart() -> str:
    return f"{stop()}; {start()}"


def status(log_lines: int = 5) -> dict:
    pid = read_pid()
    info = {"running": pid is not None, "pid": pid,
            "log_file": str(LOG_FILE), "foreign": foreign_instances()}
    if pid:
        try:
            info["uptime_seconds"] = int(time.time() - psutil.Process(pid).create_time())
        except psutil.Error:
            pass
    if LOG_FILE.exists():
        info["recent_log"] = LOG_FILE.read_text(errors="replace").splitlines()[-log_lines:]
    return info


def main() -> None:
    action = sys.argv[1] if len(sys.argv) > 1 else "status"
    force = "--force" in sys.argv
    if action == "start":
        print(start(force=force))
    elif action == "stop":
        print(stop())
    elif action == "restart":
        print(restart())
    elif action == "status":
        st = status()
        print("RUNNING" if st["running"] else "STOPPED",
              f"(pid {st['pid']})" if st["pid"] else "")
        for line in st.get("recent_log", []):
            print("  ", line)
        for pid, cmd in st["foreign"]:
            print(f"  ! other radar running: pid {pid} — {cmd[:80]}")
    elif action == "--json":
        print(json.dumps(status(), indent=2))
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
