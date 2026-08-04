#!/usr/bin/env python3
"""perfetto-serve.py — read-only CORS HTTP server for raw Perfetto traces.

The Perfetto web UI (https://ui.perfetto.dev) treats a ?url= parameter pointing
at a localhost address as a raw trace file: it fetches the URL and WASM-parses
the returned bytes. The trace_processor ``--httpd`` root is an RPC help page,
not trace bytes, so a bare ?url=http://127.0.0.1:<port> fails with "Unknown
trace type provided (ERR:fmt)". This server serves the raw *.perfetto-trace
file over HTTP with an Access-Control-Allow-Origin header so the UI's fetch
succeeds and the WASM trace processor can parse the trace.

Usage:
    perfetto-serve.py --port 9001 --dir path/to/trace-dir
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

DEFAULT_PORT = 9001
CORS_ORIGIN = "https://ui.perfetto.dev"
CHUNK_SIZE = 64 * 1024


class ShutdownRequested(Exception):
    """Raised from a signal handler to stop serve_forever cleanly."""


class TraceHandler(BaseHTTPRequestHandler):
    """Serves regular files under serve_dir read-only, with CORS headers."""

    serve_dir = ""
    protocol_version = "HTTP/1.1"

    # Log to stderr so stdout stays a single clean "Serving ..." line when the
    # server is started in the background by `make perfetto-view`.
    def log_message(self, format: str, *args: object) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), format % args))

    def send_response(self, code: int, message: str | None = None) -> None:
        # Every response (success and error alike) carries the CORS header, so
        # the https://ui.perfetto.dev page can read the response.
        super().send_response(code, message)
        self.send_header("Access-Control-Allow-Origin", CORS_ORIGIN)

    def do_GET(self) -> None:
        self._serve_file()

    def do_HEAD(self) -> None:
        self._serve_file()

    def _resolve_path(self) -> str | None:
        """Map the request path to a real file under serve_dir, rejecting traversal."""
        root = os.path.realpath(self.serve_dir)
        path = unquote(urlsplit(self.path).path)
        target = os.path.realpath(os.path.join(root, path.lstrip("/")))
        if target != root and not target.startswith(root + os.sep):
            return None
        return target

    def _serve_file(self) -> None:
        target = self._resolve_path()
        if target is None or not os.path.isfile(target):
            self.send_error(404, "not found")
            return
        try:
            size = os.path.getsize(target)
            with open(target, "rb") as fh:
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(size))
                self.end_headers()
                if self.command == "HEAD":
                    return
                while True:
                    chunk = fh.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (OSError, BrokenPipeError):
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Serve raw Perfetto traces over HTTP with CORS for the Perfetto UI."
        )
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help="port to bind on 127.0.0.1 (default: %(default)s)",
    )
    parser.add_argument(
        "--dir", required=True, help="directory containing the trace file(s) to serve"
    )
    args = parser.parse_args(argv)

    serve_dir = os.path.realpath(args.dir)
    if not os.path.isdir(serve_dir):
        parser.error("--dir is not a directory: %s" % args.dir)

    handler = type("TraceHandler", (TraceHandler,), {"serve_dir": serve_dir})
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    server.daemon_threads = True

    def _signal(signum: int, frame: object) -> None:
        raise ShutdownRequested

    signal.signal(signal.SIGINT, _signal)
    signal.signal(signal.SIGTERM, _signal)

    print("Serving %s on http://127.0.0.1:%d" % (serve_dir, args.port), flush=True)
    try:
        server.serve_forever()
    except ShutdownRequested:
        sys.stderr.write("perfetto-serve: shutting down\n")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
