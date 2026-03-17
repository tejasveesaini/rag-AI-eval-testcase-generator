from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from workflows import (
    ActionResult,
    add_app_log,
    collect_context,
    discover_workspace,
    evaluate_suite,
    fetch_story,
    generate_suite,
    get_app_logs,
    get_issue_bundle,
    get_push_history,
    push_selected_tests,
)

UI_DIR = Path(__file__).resolve().parent
ROOT_DIR = UI_DIR.parent
STATIC_DIR = UI_DIR / "static"
PROJECT_PYTHON = ROOT_DIR / ".venv" / "bin" / "python"


def _prefer_project_python() -> None:
    if os.environ.get("RAG_UI_REEXECED") == "1":
        return
    if not PROJECT_PYTHON.exists():
        return
    venv_prefix = str((ROOT_DIR / ".venv").resolve())
    if Path(sys.executable).absolute() == PROJECT_PYTHON.absolute() or sys.prefix == venv_prefix:
        return

    env = dict(os.environ)
    env["RAG_UI_REEXECED"] = "1"
    os.execve(str(PROJECT_PYTHON), [str(PROJECT_PYTHON), __file__, *sys.argv[1:]], env)


class UIRequestHandler(BaseHTTPRequestHandler):
    server_version = "RAGEvalUI/0.1"

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/":
            self._serve_file(UI_DIR / "index.html", "text/html; charset=utf-8")
            return

        if path.startswith("/static/"):
            file_path = STATIC_DIR / path.removeprefix("/static/")
            self._serve_file(file_path)
            return

        if path == "/api/workspace":
            self._send_json(discover_workspace())
            return

        if path == "/api/logs":
            self._send_json(get_app_logs())
            return

        if path.startswith("/api/issue/"):
            issue_key = unquote(path.removeprefix("/api/issue/"))
            self._send_json(get_issue_bundle(issue_key))
            return

        if path == "/api/push-history":
            from urllib.parse import parse_qs
            qs = parse_qs(parsed.query)
            issue_key = qs.get("issue", [None])[0]
            self._send_json(get_push_history(issue_key))
            return

        self._send_error_json(HTTPStatus.NOT_FOUND, "Route not found.")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        body = self._read_json_body()
        if body is None:
            self._send_error_json(HTTPStatus.BAD_REQUEST, "Request body must be valid JSON.")
            return

        action_map = {
            "/api/actions/fetch": lambda payload: fetch_story(payload.get("issue_key", "")),
            "/api/actions/collect-context": lambda payload: collect_context(payload.get("issue_key", "")),
            "/api/actions/generate": lambda payload: generate_suite(
                payload.get("issue_key", ""),
                payload.get("mode", "baseline"),
                max_tests=int(payload.get("max_tests", 10)),
                offset=int(payload.get("offset", 0)),
            ),
            "/api/actions/evaluate": lambda payload: evaluate_suite(payload.get("issue_key", ""), payload.get("mode", "baseline")),
            "/api/actions/push": lambda payload: push_selected_tests(
                payload.get("issue_key", ""),
                payload.get("mode", "baseline"),
                payload.get("indices", []),
            ),
        }

        action = action_map.get(parsed.path)
        if action is None:
            self._send_error_json(HTTPStatus.NOT_FOUND, "Route not found.")
            return

        result = action(body)
        status = HTTPStatus.OK if result.ok else HTTPStatus.BAD_REQUEST
        self._send_json(result.to_dict(), status=status)

    def log_message(self, fmt: str, *args) -> None:
        message = f"{self.address_string()} - {fmt % args}"
        print(f"[ui] {message}")

    def _read_json_body(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None

        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return None

    def _serve_file(self, path: Path, content_type: str | None = None) -> None:
        if not path.exists() or not path.is_file():
            self._send_error_json(HTTPStatus.NOT_FOUND, "File not found.")
            return

        body = path.read_bytes()
        guessed_type = content_type or mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", guessed_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, status: HTTPStatus, message: str) -> None:
        result = ActionResult(ok=False, message=message)
        self._send_json(result.to_dict(), status=status)


def main() -> None:
    parser = argparse.ArgumentParser(description="Standalone UI for the RAG AI Eval Testcase Generator.")
    parser.add_argument("--host", default="127.0.0.1", help="Host interface to bind.")
    parser.add_argument("--port", type=int, default=8090, help="Port to serve on.")
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), UIRequestHandler)
    print(f"RAG AI Eval UI running on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down UI server.")
    finally:
        server.server_close()


if __name__ == "__main__":
    _prefer_project_python()
    main()
