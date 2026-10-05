"""HTTP front-end for the MIDI timeline normalizer.

Exposes ``POST /api/midi/normalize`` (raw SMF bytes in, JSON timeline out)
and ``GET /health``.  Standard library only, so the image builds with no
package installation step.
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .midi import MAX_FILE_BYTES, MidiError, normalize

NORMALIZE_PATH = "/api/midi/normalize"
_DRAIN_CAP = 32 << 20  # never read more than this when rejecting oversize bodies


class Handler(BaseHTTPRequestHandler):
    server_version = "MidiNormalizer/1.0"
    protocol_version = "HTTP/1.1"

    # -- helpers ---------------------------------------------------------

    def _send_json(self, status: int, payload: dict, close: bool = False) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, code: str, message: str, offset=None,
               close: bool = False) -> None:
        self._send_json(
            status,
            {"error": {"code": code, "message": message, "offset": offset}},
            close=close,
        )

    def _drain(self, length: int) -> None:
        """Discard up to ``length`` body bytes (bounded) to keep the socket sane."""
        remaining = min(length, _DRAIN_CAP)
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                break
            remaining -= len(chunk)

    # -- routes ----------------------------------------------------------

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/health":
            self._send_json(200, {"status": "ok"})
        elif path == NORMALIZE_PATH:
            self._error(405, "method_not_allowed", "use POST for this endpoint")
        else:
            self._error(404, "not_found", f"unknown path: {path}")

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if path != NORMALIZE_PATH:
            self._error(404, "not_found", f"unknown path: {path}")
            return

        length_header = self.headers.get("Content-Length")
        if length_header is None:
            self._error(411, "length_required", "Content-Length header is required")
            return
        try:
            length = int(length_header)
        except ValueError:
            self._error(400, "bad_content_length", "Content-Length is not an integer")
            return
        if length < 0:
            self._error(400, "bad_content_length", "Content-Length must not be negative")
            return
        if length > MAX_FILE_BYTES:
            self._drain(length)
            self._error(
                413,
                "payload_too_large",
                f"body is {length} bytes, limit is {MAX_FILE_BYTES} (1 MiB)",
                offset=MAX_FILE_BYTES,
                close=True,
            )
            return

        body = self.rfile.read(length)
        if len(body) < length:
            self._error(
                400,
                "truncated_upload",
                f"client sent {len(body)} of {length} declared bytes",
                offset=len(body),
            )
            return

        try:
            result = normalize(body)
        except MidiError as exc:
            # Structural failure: report the locating offset, never a
            # partial timeline.
            self._error(400, exc.code, exc.message, offset=exc.offset)
            return
        self._send_json(200, result)


def main() -> None:
    port = int(os.environ.get("PORT", "8000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"midi-normalizer listening on 0.0.0.0:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
