#!/usr/bin/env python3
"""Serve a live, read-only viewer for local Codex article runs."""

import argparse
import json
import os
import re
import stat
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


ASSETS = Path(__file__).resolve().with_name("log_browser")
RUN_NAME = re.compile(r"work_article_(\d+)_(\d+)")
LOG_SUFFIXES = {".log", ".jsonl", ".ndjson"}
ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")
MARKERS = {
    "user": "user", "codex": "assistant", "assistant": "assistant",
    "thinking": "reasoning", "analysis": "reasoning", "reasoning": "reasoning",
    "exec": "tool", "tool": "tool", "web search": "tool", "mcp": "tool",
    "tokens used": "usage", "file update": "file", "apply_patch": "file",
    "system": "system", "error": "error",
}
JSON_TYPES = {
    **MARKERS,
    "user_message": "user", "agent_message": "assistant",
    "agent_reasoning": "reasoning", "agent_reasoning_raw_content": "reasoning",
    "token_count": "usage", "token_usage_record": "usage",
    "command_execution": "tool", "web_search_call": "tool",
    "file_change": "file", "turn.failed": "error",
}
ASSET_TYPES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}


def plain_messages(data):
    messages = []
    category, start, lines = "system", 1, []
    for number, line in enumerate(ANSI.sub("", data).splitlines(), 1):
        marker = MARKERS.get(line.lower()) if line == line.strip() else None
        if marker:
            if body := "\n".join(lines).strip():
                messages.append({"type": category, "line": start, "text": body})
            category, start, lines = marker, number, []
        else:
            lines.append(line)
    if body := "\n".join(lines).strip():
        messages.append({"type": category, "line": start, "text": body})
    return messages


def get_object(value):
    return value if isinstance(value, dict) else {}


def get_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(filter(None, (get_text(get_object(part).get("text")) for part in value)))
    return ""


def json_message(record, line):
    outer = str(record.get("type") or "record")
    payload = get_object(record.get("payload"))
    item = get_object(record.get("item") or payload.get("item"))
    body = item or payload or record
    event = str(body.get("type") or outer)
    if event == "message":
        category = JSON_TYPES.get(body.get("role"), "system") if isinstance(body.get("role"), str) else "system"
    else:
        category = JSON_TYPES.get(event)
        if not category:
            if event.endswith(("_call", "_call_output")) or event.startswith(("exec_command_", "mcp_tool_", "web_search_")):
                category = "tool"
            elif event.startswith("patch_apply_"):
                category = "file"
            elif "error" in event or event.endswith(".failed"):
                category = "error"
            else:
                category = "system"

    content = {key: value for key, value in body.items() if key != "encrypted_content"}
    message = get_text(body.get("text")) or get_text(body.get("message")) or get_text(body.get("content"))
    if not message and category == "reasoning":
        message = get_text(body.get("summary"))
    timestamp = record.get("timestamp")
    if isinstance(timestamp, str):
        try:
            timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            timestamp = timestamp.isoformat() if timestamp.tzinfo else None
        except ValueError:
            timestamp = None
    else:
        timestamp = None
    detail = f"{outer} · {event}" if outer != event else event
    text = message or json.dumps(content, ensure_ascii=False, indent=2)
    return {"type": category, "detail": detail, "line": line, "time": timestamp, "text": text}


def parse_messages(data, suffix):
    is_json = suffix in {".jsonl", ".ndjson"}
    if not is_json and data.lstrip().startswith("{"):
        first_line = data.lstrip().partition("\n")[0]
        try:
            is_json = isinstance(json.loads(first_line), dict)
        except ValueError:
            pass
    if not is_json:
        return list(reversed(plain_messages(data)))

    messages = []
    lines = data.split("\n")
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            if number < len(lines):
                messages.append({"type": "system", "line": number, "text": line})
            continue
        if isinstance(record, dict):
            messages.append(json_message(record, number))
    # The append order is authoritative for mixed streams and records without timestamps.
    return list(reversed(messages))


def read_regular(path):
    """Open a regular file without following its final component if it is a symlink."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    stream = os.fdopen(fd, "rb")
    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
        stream.close()
        raise OSError("Expected a regular log file")
    return stream


class Catalog:
    def __init__(self, root, codex_home):
        self.root = root.resolve()
        codex_home = codex_home.resolve()
        self.session_roots = [codex_home / "sessions", codex_home / "archived_sessions"]
        self.session_cwds = {}
        self.files = {}
        self.lock = threading.Lock()

    def session_cwd(self, path, info):
        identity = (info.st_dev, info.st_ino)
        cached = self.session_cwds.get(path)
        if cached and cached[0] == identity:
            return cached[1]

        with read_regular(path) as stream:
            first = stream.readline()
        if not first.endswith(b"\n"):
            return None  # Codex can still be writing the session header.
        try:
            record = get_object(json.loads(first))
        except ValueError:
            return None
        cwd = get_object(record.get("payload")).get("cwd")
        relative = None
        if record.get("type") == "session_meta" and isinstance(cwd, str):
            try:
                relative = Path(cwd).resolve().relative_to(self.root)
            except (ValueError, OSError, RuntimeError):
                pass
        self.session_cwds[path] = (identity, relative)
        return relative

    def snapshot(self):
        with self.lock:
            runs, files = {}, {}

            def run_for(name):
                match = RUN_NAME.fullmatch(name)
                if not match:
                    return None
                return runs.setdefault(name, {
                    "name": name, "article": int(match[1]), "timestamp": int(match[2]), "logs": [],
                })

            def add_log(run, path, scope, name, info, source):
                if not stat.S_ISREG(info.st_mode):
                    return
                log_id = f"{source}:{path.relative_to(scope).as_posix()}"
                run["logs"].append({
                    "id": log_id, "name": name, "modified": info.st_mtime, "source": source,
                })
                files[log_id] = (path, scope)

            for directory in self.root.iterdir():
                if not RUN_NAME.fullmatch(directory.name) or directory.is_symlink() or not directory.is_dir():
                    continue
                run = run_for(directory.name)
                for folder in (directory / "codex_logs", directory / "logs", directory):
                    if folder.is_symlink() or not folder.is_dir():
                        continue
                    try:
                        for path in folder.iterdir():
                            if path.suffix.lower() in LOG_SUFFIXES:
                                try:
                                    add_log(run, path, self.root, path.relative_to(directory).as_posix(),
                                            path.lstat(), "file")
                                except OSError:
                                    continue
                    except FileNotFoundError:
                        continue

            sessions = set()
            for scope in self.session_roots:
                source = "session" if scope.name == "sessions" else "archive"
                for path in scope.rglob("rollout-*.jsonl"):
                    try:
                        info = path.lstat()
                        if not stat.S_ISREG(info.st_mode) or path.resolve() != path:
                            continue
                        sessions.add(path)
                        cwd = self.session_cwd(path, info)
                        if cwd and cwd.parts and (run := run_for(cwd.parts[0])) is not None:
                            add_log(run, path, scope, path.name, info, source)
                    except OSError:
                        continue

            self.session_cwds = {path: value for path, value in self.session_cwds.items() if path in sessions}
            for run in runs.values():
                run["logs"].sort(key=lambda log: (log["modified"], log["id"]), reverse=True)
            self.files = files
            return sorted(runs.values(), key=lambda run: (run["timestamp"], run["article"]), reverse=True)

    def read(self, log_id, revision):
        with self.lock:
            entry = self.files.get(log_id)
        if entry is None:
            raise FileNotFoundError
        path, scope = entry
        if path.resolve() != path or not path.is_relative_to(scope):
            raise FileNotFoundError
        with read_regular(path) as stream:
            info = os.fstat(stream.fileno())
            current = f"{info.st_dev}:{info.st_ino}:{info.st_size}:{info.st_mtime_ns}"
            if revision == current:
                return {"revision": current}
            data = stream.read(info.st_size).decode("utf-8", errors="replace")
        return {"revision": current, "messages": parse_messages(data, path.suffix.lower())}


def handler_for(catalog):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            host = self.headers.get("Host")
            allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            origin = self.headers.get("Origin")
            if host not in allowed or (origin and origin != f"http://{host}"):
                self.send_json({"error": "Open the viewer using its localhost address."}, 403)
                return
            try:
                url = urlsplit(self.path)
                if url.path == "/api/runs":
                    self.send_json({"runs": catalog.snapshot()})
                elif url.path == "/api/messages":
                    query = parse_qs(url.query)
                    self.send_json(catalog.read(query.get("log", [""])[0], query.get("revision", [""])[0]))
                elif url.path in ASSET_TYPES:
                    name, content_type = ASSET_TYPES[url.path]
                    self.respond((ASSETS / name).read_bytes(), content_type)
                else:
                    self.send_json({"error": "Page not found."}, 404)
            except FileNotFoundError:
                self.send_json({"error": "This log is no longer available."}, 404)
            except OSError:
                self.send_json({"error": "Unable to read the run directory or log."}, 500)

        def send_json(self, value, status=200):
            self.respond(json.dumps(value).encode(), "application/json; charset=utf-8", status)

        def respond(self, data, content_type, status=200):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; object-src 'none'; frame-ancestors 'none'")
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_args):
            pass

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent,
                        help="parent directory of the work_article_* run folders")
    parser.add_argument("--port", type=int, default=8765, help="localhost HTTP port (default: 8765)")
    parser.add_argument("--codex-home", type=Path,
                        default=Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex"),
                        help="Codex home for matching CLI sessions")
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    if not root.is_dir():
        parser.error("--root must point to an existing directory")
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    catalog = Catalog(root, args.codex_home.expanduser().resolve())
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), handler_for(catalog))
    except OSError as exc:
        parser.exit(1, f"Cannot start the Codex log viewer: {exc}\n")
    with server:
        print(f"Codex logs: http://127.0.0.1:{server.server_port} (root: {root})", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
