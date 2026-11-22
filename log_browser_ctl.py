#!/usr/bin/env python3
"""Run the local Codex log viewer as a detached process."""

import argparse
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


PROJECT = Path(__file__).resolve().parent
RUNTIME = PROJECT / ".log_browser"
LOG = RUNTIME / "server.log"
STATE = RUNTIME / "state.json"
TIMEOUT = 5


def running(server_lock):
    try:
        fcntl.flock(server_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    fcntl.flock(server_lock, fcntl.LOCK_UN)
    return False


def start(server_lock, options):
    if running(server_lock):
        print_status("Already running")
        return 0

    STATE.unlink(missing_ok=True)
    fcntl.flock(server_lock, fcntl.LOCK_EX)
    with LOG.open("ab") as output, LOG.open("rb") as reader:
        reader.seek(0, os.SEEK_END)
        process = subprocess.Popen(
            [sys.executable, str(PROJECT / "log_browser.py"), *options],
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            pass_fds=(server_lock.fileno(),),
        )
        try:
            deadline = time.monotonic() + TIMEOUT
            while time.monotonic() < deadline and process.poll() is None:
                line = reader.readline().decode("utf-8", errors="replace")
                if line.startswith("Codex logs: http://127.0.0.1:"):
                    url = line.split()[2]
                    STATE.write_text(json.dumps({"pid": process.pid, "url": url}), encoding="utf-8")
                    print_status("Started")
                    return 0
                time.sleep(0.05)
            raise RuntimeError(f"The viewer did not start. See {LOG}")
        except BaseException:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=TIMEOUT)
            STATE.unlink(missing_ok=True)
            raise


def read_state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError) as exc:
        raise RuntimeError(f"The viewer is running but its state is unavailable. See {LOG}") from exc


def print_status(label):
    state = read_state()
    print(f"{label} at {state['url']} (PID {state['pid']})")
    print(f"Server log: {LOG}")


def stop(server_lock):
    if not running(server_lock):
        STATE.unlink(missing_ok=True)
        print("The viewer is already stopped.")
        return 0

    try:
        os.kill(read_state()["pid"], signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + TIMEOUT
    while running(server_lock):
        if time.monotonic() >= deadline:
            raise RuntimeError(f"The viewer did not stop within {TIMEOUT} seconds. See {LOG}")
        time.sleep(0.05)
    STATE.unlink(missing_ok=True)
    print("Stopped the viewer.")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", choices=("start", "status", "stop"), default="start")
    parser.add_argument("--root", help="parent directory of the article run folders")
    parser.add_argument("--port", type=int, help="localhost HTTP port (default: 8765)")
    parser.add_argument("--codex-home", help="Codex home for matching CLI sessions")
    args = parser.parse_args()

    options = []
    for name in ("root", "port", "codex_home"):
        if (value := getattr(args, name)) is not None:
            options.extend(("--" + name.replace("_", "-"), str(value)))
    if args.action != "start" and options:
        parser.error("--root, --port, and --codex-home apply to start")

    try:
        RUNTIME.mkdir(mode=0o700, exist_ok=True)
        with (RUNTIME / "controller.lock").open("a") as controller_lock:
            fcntl.flock(controller_lock, fcntl.LOCK_EX)
            with (RUNTIME / "server.lock").open("a") as server_lock:
                if args.action == "start":
                    return start(server_lock, options)
                if args.action == "stop":
                    return stop(server_lock)
                if running(server_lock):
                    print_status("Running")
                    return 0
                print("The viewer is stopped.")
                return 1
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        parser.exit(1, f"{parser.prog}: {exc}\n")


if __name__ == "__main__":
    sys.exit(main())
