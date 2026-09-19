#!/usr/bin/env python3
"""The HTTP slot this service declares, and nothing else.

`http.server` from the standard library, deliberately. The alternative is a
framework and its dependency tree, and this service's whole argument is that the
thing opening an untrusted URL should be small enough to read: adding forty packages
to route two paths would be arguing against the README.

Two endpoints, because the spec declares one port and the service does one thing:

    GET  /health      -> 200, always, as long as the process is answering
    POST /transcribe  -> {"url": "..."} -> the transcript

Concurrency is **one request at a time**, by a semaphore rather than by the server
being single-threaded. Transcription is CPU-bound and the instance declares a CPU
quota; two concurrent whisper runs on 2 vCPU do not go twice as fast, they go twice
as slow each and double the peak disk. A second caller is told 503 with a
`Retry-After` immediately, which is a better answer than a request that silently
queues past its own timeout.
"""

import json
import os
import signal
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import ThreadingMixIn

import config
import pipeline
import urls

# Bound on the request body. A `{"url": ...}` object is a few hundred bytes; anything
# past this is not one, and reading it to find out is the thing to avoid.
MAX_BODY_BYTES = 8192

_slot = threading.BoundedSemaphore(1)


def log(message: str) -> None:
    """One line, stderr, flushed.

    stderr because stdout is where a future version might stream a transcript, and
    flushed because a service whose logs appear only when its buffer fills is a
    service you cannot watch start.
    """
    sys.stderr.write(f"[yt-transcript] {message}\n")
    sys.stderr.flush()


class Handler(BaseHTTPRequestHandler):
    # HTTP/1.1 so keep-alive works and a caller is not reconnecting per request.
    protocol_version = "HTTP/1.1"
    server_version = "yt-transcript"
    sys_version = ""

    # BaseHTTPRequestHandler's default writes to stderr with its own format and,
    # more to the point, logs the full request line -- which carries a
    # caller-controlled URL. Requests are logged, by their outcome, in `_respond`.
    def log_message(self, fmt, *args):  # noqa: A003 - name fixed by the base class
        pass

    def _respond(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if status == HTTPStatus.SERVICE_UNAVAILABLE:
            self.send_header("Retry-After", "30")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str, detail: str = "") -> None:
        payload = {"error": message}
        if detail:
            payload["detail"] = detail
        self._respond(status, payload)

    def do_GET(self):  # noqa: N802 - name fixed by the base class
        if self.path.split("?", 1)[0] == "/health":
            self._respond(HTTPStatus.OK, {
                "status": "ok",
                "model": os.path.basename(config.MODEL_PATH),
                "max_duration_s": self.server.config.max_duration_s,
                "language": self.server.config.language,
                "threads": self.server.config.threads,
                "busy": not _slot._value,  # noqa: SLF001 - the only way to read it
            })
            return
        self._error(HTTPStatus.NOT_FOUND, "no such endpoint", "GET /health")

    def do_POST(self):  # noqa: N802 - name fixed by the base class
        if self.path.split("?", 1)[0] != "/transcribe":
            self._error(HTTPStatus.NOT_FOUND, "no such endpoint", "POST /transcribe")
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._error(HTTPStatus.BAD_REQUEST, "Content-Length is not a number")
            return
        if length <= 0:
            self._error(HTTPStatus.BAD_REQUEST, "a JSON body is required",
                        '{"url": "https://www.youtube.com/watch?v=..."}')
            return
        if length > MAX_BODY_BYTES:
            self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                        f"the body is larger than {MAX_BODY_BYTES} bytes")
            return

        try:
            raw = self.rfile.read(length)
        except OSError:
            return

        try:
            document = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._error(HTTPStatus.BAD_REQUEST, "the body is not valid JSON")
            return
        if not isinstance(document, dict):
            self._error(HTTPStatus.BAD_REQUEST, "the body is not a JSON object")
            return

        try:
            url = urls.validate(document.get("url"))
        except urls.UrlError as e:
            # 400 and not 403: the caller sent a bad request, and telling them which
            # hosts are accepted is the whole of the useful answer.
            self._error(HTTPStatus.BAD_REQUEST, str(e))
            return

        identifier = urls.video_id(url) or "<no id>"

        if not _slot.acquire(blocking=False):
            log(f"busy, refused {identifier}")
            self._error(HTTPStatus.SERVICE_UNAVAILABLE,
                        "this instance is already transcribing a video",
                        "one request at a time; retry shortly")
            return

        try:
            log(f"transcribing {identifier}")
            result = pipeline.run(url, self.server.config)
        except pipeline.PipelineError as e:
            log(f"failed {identifier}: {e} ({e.detail})" if e.detail
                else f"failed {identifier}: {e}")
            self._error(e.status, str(e), e.detail)
            return
        except Exception as e:  # noqa: BLE001 - the process must survive a request
            log(f"internal error on {identifier}: {type(e).__name__}: {e}")
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal error")
            return
        finally:
            _slot.release()

        log(
            f"done {identifier}: {len(result['segments'])} segments, "
            f"{len(result['text'])} chars, {result['elapsed_s']}s"
        )
        self._respond(HTTPStatus.OK, result)


class Server(ThreadingHTTPServer):
    # Threaded so /health answers while a transcription is running -- the semaphore
    # is what serialises the *work*, and a health check that blocked behind an hour
    # of audio would tell a node this instance had died.
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, cfg):
        self.config = cfg
        super().__init__(address, handler)


def main() -> int:
    try:
        cfg = config.load()
    except config.ConfigError as e:
        log(f"FATAL: {e}")
        return 2

    for path in (config.YTDLP_BIN, config.FFMPEG_BIN, config.WHISPER_BIN, config.MODEL_PATH):
        if not os.path.exists(path):
            # Refused at start rather than on the first request: an instance that
            # cannot possibly work should not be one a node reports as running.
            log(f"FATAL: {path} is missing from this image")
            return 2

    # 0.0.0.0 because the node reaches this on the guest's own address; there is no
    # loopback-only case for a service whose entire purpose is to be called.
    server = Server(("0.0.0.0", cfg.port), Handler, cfg)

    # SIGTERM has to be handled explicitly, and this is not boilerplate. Under nodo's
    # microVM the entrypoint is execed by /init straight out of switch_root, so this
    # process is **PID 1** -- and PID 1 has no default signal dispositions. An
    # unhandled SIGTERM is discarded rather than fatal, so without this the process
    # ignores every polite request to stop and is eventually killed: under `docker
    # stop`, ten seconds later with SIGKILL, leaving a half-written temp directory.
    def stop(signum, _frame):
        log(f"signal {signum}, shutting down")
        # From a signal handler, so it must not be `shutdown()` on this thread:
        # serve_forever is running here, and shutdown() would deadlock waiting for
        # the loop that is currently in this handler.
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    log(
        f"listening on :{cfg.port} "
        f"(model={os.path.basename(config.MODEL_PATH)}, threads={cfg.threads}, "
        f"language={cfg.language}, max_duration_s={cfg.max_duration_s})"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
