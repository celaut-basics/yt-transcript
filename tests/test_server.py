"""The HTTP contract, against a real server on a real socket.

The server is started on port 0 (the kernel picks a free one) with the pipeline
replaced, so these exercise routing, body handling, status codes and the
one-at-a-time semaphore without downloading or transcribing anything.
"""

import http.client
import json
import socket
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

import config
import pipeline
import server


def a_config(**over):
    values = dict(
        port=0,
        max_duration_s=3600,
        threads=4,
        language="auto",
        request_timeout_s=600,
    )
    values.update(over)
    return config.Config(**values)


class ServerFixture(unittest.TestCase):
    def setUp(self):
        self.httpd = server.Server(("127.0.0.1", 0), server.Handler, a_config())
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        # A short poll interval only makes shutdown() return sooner between tests.
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05},
            daemon=True,
        )
        self.thread.start()
        self.addCleanup(self._stop)

    def _stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    def request(self, method, path, body=None, timeout=10):
        data = None
        headers = {}
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            self.base + path, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return response.status, json.loads(response.read().decode())
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, {"raw": raw}


class TestHealth(ServerFixture):
    def test_health_is_200_and_describes_the_instance(self):
        status, body = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["max_duration_s"], 3600)
        self.assertEqual(body["model"], "ggml-base.bin")
        self.assertFalse(body["busy"])

    def test_health_needs_no_body(self):
        status, _ = self.request("GET", "/health")
        self.assertEqual(status, 200)

    def test_a_query_string_does_not_change_the_route(self):
        status, _ = self.request("GET", "/health?probe=1")
        self.assertEqual(status, 200)


class TestRouting(ServerFixture):
    def test_unknown_paths_are_404(self):
        for method, path in (
            ("GET", "/"),
            ("GET", "/transcribe"),
            ("POST", "/health"),
            ("POST", "/anything"),
        ):
            with self.subTest(method=method, path=path):
                status, _ = self.request(
                    method, path, body={} if method == "POST" else None
                )
                self.assertEqual(status, 404)


class TestRequestValidation(ServerFixture):
    def test_a_bad_host_is_400_and_says_which_hosts_are_accepted(self):
        status, body = self.request(
            "POST", "/transcribe",
            body={"url": "https://youtube.com.attacker.example/watch?v=x"},
        )
        self.assertEqual(status, 400)
        self.assertIn("youtube.com.attacker.example", body["error"])
        self.assertIn("www.youtube.com", body["error"])

    def test_a_file_url_is_400(self):
        status, _ = self.request(
            "POST", "/transcribe", body={"url": "file:///etc/passwd"}
        )
        self.assertEqual(status, 400)

    def test_a_missing_url_is_400(self):
        status, _ = self.request("POST", "/transcribe", body={})
        self.assertEqual(status, 400)

    def test_a_non_string_url_is_400(self):
        status, _ = self.request("POST", "/transcribe", body={"url": 42})
        self.assertEqual(status, 400)

    def test_an_empty_body_is_400(self):
        status, _ = self.request("POST", "/transcribe", body=b"")
        self.assertEqual(status, 400)

    def test_a_non_json_body_is_400(self):
        status, _ = self.request("POST", "/transcribe", body=b"not json")
        self.assertEqual(status, 400)

    def test_a_json_array_body_is_400(self):
        status, _ = self.request("POST", "/transcribe", body=b'["url"]')
        self.assertEqual(status, 400)

    def test_an_oversized_body_is_413(self):
        status, _ = self.request(
            "POST", "/transcribe",
            body=json.dumps({"url": "x" * 20000}).encode(),
        )
        self.assertEqual(status, 413)

    def test_a_refused_url_never_reaches_the_pipeline(self):
        """The boundary is checked before any work is scheduled."""
        with mock.patch.object(pipeline, "run") as run:
            status, _ = self.request(
                "POST", "/transcribe", body={"url": "https://evil.example/x"}
            )
        self.assertEqual(status, 400)
        run.assert_not_called()


class TestSuccess(ServerFixture):
    def test_a_good_request_returns_the_documented_shape(self):
        payload = {
            "text": "hello there",
            "segments": [{"start_ms": 0, "end_ms": 3000, "text": "hello there"}],
            "language": "en",
            "video_id": "dQw4w9WgXcQ",
            "duration_s": 3.0,
            "elapsed_s": 1.2,
        }
        with mock.patch.object(pipeline, "run", return_value=payload):
            status, body = self.request(
                "POST", "/transcribe",
                body={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
            )
        self.assertEqual(status, 200)
        self.assertEqual(body, payload)

    def test_the_validated_url_is_what_the_pipeline_is_given(self):
        url = "https://youtu.be/dQw4w9WgXcQ"
        with mock.patch.object(pipeline, "run", return_value={
            "text": "", "segments": [], "language": None,
            "video_id": None, "duration_s": 0, "elapsed_s": 0,
        }) as run:
            self.request("POST", "/transcribe", body={"url": url})
        self.assertEqual(run.call_args[0][0], url)


class TestFailures(ServerFixture):
    def test_a_pipeline_error_keeps_its_status(self):
        for status_code in (413, 422, 502, 504):
            with self.subTest(status=status_code):
                with mock.patch.object(
                    pipeline, "run",
                    side_effect=pipeline.PipelineError("nope", status=status_code,
                                                       detail="because"),
                ):
                    status, body = self.request(
                        "POST", "/transcribe",
                        body={"url": "https://youtu.be/dQw4w9WgXcQ"},
                    )
                self.assertEqual(status, status_code)
                self.assertEqual(body["error"], "nope")
                self.assertEqual(body["detail"], "because")

    def test_an_unexpected_exception_is_500_and_leaks_nothing(self):
        with mock.patch.object(
            pipeline, "run", side_effect=RuntimeError("/secret/path blew up"),
        ):
            status, body = self.request(
                "POST", "/transcribe",
                body={"url": "https://youtu.be/dQw4w9WgXcQ"},
            )
        self.assertEqual(status, 500)
        self.assertEqual(body["error"], "internal error")
        self.assertNotIn("secret", json.dumps(body))

    def test_the_server_survives_a_failed_request(self):
        with mock.patch.object(pipeline, "run", side_effect=RuntimeError("boom")):
            self.request("POST", "/transcribe",
                         body={"url": "https://youtu.be/dQw4w9WgXcQ"})
        status, _ = self.request("GET", "/health")
        self.assertEqual(status, 200)


class TestConcurrency(ServerFixture):
    def test_a_second_request_is_refused_with_503_and_retry_after(self):
        """One at a time: two whisper runs on 2 vCPU are slower, not faster."""
        entered = threading.Event()
        release = threading.Event()

        def slow(url, cfg, **kwargs):
            entered.set()
            release.wait(timeout=10)
            return {"text": "", "segments": [], "language": None,
                    "video_id": None, "duration_s": 0, "elapsed_s": 0}

        with mock.patch.object(pipeline, "run", slow):
            first = threading.Thread(
                target=self.request,
                args=("POST", "/transcribe",
                      {"url": "https://youtu.be/dQw4w9WgXcQ"}),
                daemon=True,
            )
            first.start()
            self.assertTrue(entered.wait(timeout=10), "first request never started")

            status, body = self.request(
                "POST", "/transcribe",
                body={"url": "https://youtu.be/dQw4w9WgXcQ"},
            )
            self.assertEqual(status, 503)
            self.assertIn("already transcribing", body["error"])

            release.set()
            first.join(timeout=10)

    def test_health_answers_while_a_transcription_holds_the_slot(self):
        """A health check that queued behind an hour of audio would read as dead."""
        entered = threading.Event()
        release = threading.Event()

        def slow(url, cfg, **kwargs):
            entered.set()
            release.wait(timeout=10)
            return {"text": "", "segments": [], "language": None,
                    "video_id": None, "duration_s": 0, "elapsed_s": 0}

        with mock.patch.object(pipeline, "run", slow):
            worker = threading.Thread(
                target=self.request,
                args=("POST", "/transcribe",
                      {"url": "https://youtu.be/dQw4w9WgXcQ"}),
                daemon=True,
            )
            worker.start()
            self.assertTrue(entered.wait(timeout=10))

            status, body = self.request("GET", "/health", timeout=5)
            self.assertEqual(status, 200)
            self.assertTrue(body["busy"])

            release.set()
            worker.join(timeout=10)

    def test_the_slot_is_released_after_a_failure(self):
        """Otherwise one failed request wedges the instance forever."""
        with mock.patch.object(pipeline, "run", side_effect=RuntimeError("boom")):
            self.request("POST", "/transcribe",
                         body={"url": "https://youtu.be/dQw4w9WgXcQ"})
        with mock.patch.object(pipeline, "run", return_value={
            "text": "ok", "segments": [], "language": None,
            "video_id": None, "duration_s": 0, "elapsed_s": 0,
        }):
            status, body = self.request(
                "POST", "/transcribe",
                body={"url": "https://youtu.be/dQw4w9WgXcQ"},
            )
        self.assertEqual(status, 200)
        self.assertEqual(body["text"], "ok")


class TestConnections(ServerFixture):
    """What one TCP connection can and cannot do to the server."""

    def _raw(self, data, timeout=5):
        """Send bytes on a fresh connection and read until the server closes it."""
        port = self.httpd.server_address[1]
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
            sock.sendall(data)
            chunks = []
            while True:
                try:
                    chunk = sock.recv(65536)
                except socket.timeout:
                    self.fail("the server kept the connection open")
                if not chunk:
                    break
                chunks.append(chunk)
        return b"".join(chunks)

    def test_keep_alive_serves_several_requests(self):
        conn = http.client.HTTPConnection(
            "127.0.0.1", self.httpd.server_address[1], timeout=5
        )
        try:
            for _ in range(3):
                conn.request("GET", "/health")
                response = conn.getresponse()
                response.read()
                self.assertEqual(response.status, 200)
        finally:
            conn.close()

    def test_an_unread_body_is_not_parsed_as_the_next_request(self):
        """A 404 sent before the body is read must close the connection.

        Otherwise the body is read as the next request line, and a client can
        smuggle a second request inside the first one's body.
        """
        smuggled = b"GET /health HTTP/1.1\r\nHost: x\r\n\r\n"
        request = (
            b"POST /not-an-endpoint HTTP/1.1\r\nHost: x\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: " + str(len(smuggled)).encode() + b"\r\n\r\n"
            + smuggled
        )
        reply = self._raw(request)
        self.assertEqual(reply.count(b"HTTP/1.1 "), 1, reply)
        self.assertIn(b" 404 ", reply)
        self.assertIn(b"Connection: close", reply)

    def test_an_oversized_body_closes_the_connection(self):
        request = (
            b"POST /transcribe HTTP/1.1\r\nHost: x\r\n"
            b"Content-Length: 999999\r\n\r\n"
        )
        reply = self._raw(request)
        self.assertIn(b" 413 ", reply)
        self.assertIn(b"Connection: close", reply)

    def test_connections_have_an_idle_timeout(self):
        """The base class default is None: a socket that waits for ever."""
        self.assertIsNotNone(server.Handler.timeout)
        self.assertLessEqual(server.Handler.timeout, 60)

    def test_a_silent_client_does_not_hold_a_thread_for_ever(self):
        """A Content-Length with no body: the read times out and the socket closes."""
        with mock.patch.object(server.Handler, "timeout", 0.3), \
                mock.patch.object(pipeline, "run") as run:
            reply = self._raw(
                b"POST /transcribe HTTP/1.1\r\nHost: x\r\n"
                b"Content-Length: 100\r\n\r\n",
                timeout=5,
            )
        self.assertEqual(reply, b"")
        run.assert_not_called()

    def test_a_short_body_is_not_parsed(self):
        """The client closes its side before the declared length arrives."""
        port = self.httpd.server_address[1]
        with mock.patch.object(pipeline, "run") as run:
            with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
                sock.sendall(
                    b"POST /transcribe HTTP/1.1\r\nHost: x\r\n"
                    b"Content-Length: 100\r\n\r\n{\"url\": \"https://youtu.be/x\"}"
                )
                sock.shutdown(socket.SHUT_WR)
                self.assertEqual(sock.recv(65536), b"")
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
