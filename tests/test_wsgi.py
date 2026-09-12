from __future__ import annotations

from io import BytesIO, StringIO
import importlib.util
import json
import logging
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from unittest.mock import patch
from wsgiref.simple_server import WSGIRequestHandler, make_server
from wsgiref.util import setup_testing_defaults
from wsgiref.validate import validator

import test_auth as auth_http_tests
import test_http as http_tests
from jobandkill.server import MAX_REQUEST_BYTES, WEB_ROOT
from jobandkill.wsgi import create_app


class QuietWSGIRequestHandler(WSGIRequestHandler):
    def log_message(self, format, *args):
        pass


def wsgi_server(address, db_path):
    return make_server(*address, validator(create_app(db_path)), handler_class=QuietWSGIRequestHandler)


class WSGIAuthParityTests(auth_http_tests.AuthHttpTests):
    """Run the unchanged authentication/transaction/privacy HTTP suite on WSGI."""

    def setUp(self):
        with patch.object(auth_http_tests, "JobAndKillServer", side_effect=wsgi_server):
            super().setUp()


class WSGIHttpParityTests(http_tests.HttpTests):
    def setUp(self):
        with patch.object(http_tests, "JobAndKillServer", side_effect=wsgi_server):
            super().setUp()


class WSGITransportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.environment = patch.dict(os.environ, {"JOBNKILL_ENV": "test", "JOBNKILL_QUIET": "1"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.app = validator(create_app(Path(self.directory.name) / "wsgi.db"))

    def request(self, path="/api/health", method="GET", body=b"", extra=None):
        environ = {}
        setup_testing_defaults(environ)
        environ.update({
            "PATH_INFO": path, "QUERY_STRING": "", "REQUEST_METHOD": method,
            "REMOTE_ADDR": "127.0.0.1", "wsgi.input": BytesIO(body),
            "CONTENT_LENGTH": str(len(body)), "CONTENT_TYPE": "application/json",
        })
        environ.update(extra or {})
        result = {}

        def start_response(status, headers, exc_info=None):
            result.update(status=int(status.split()[0]), headers=headers)

        response = self.app(environ, start_response)
        try:
            result["body"] = b"".join(response)
        finally:
            if hasattr(response, "close"):
                response.close()
        return result

    def test_health_discloses_only_status(self):
        response = self.request()
        self.assertEqual(response["status"], 200)
        self.assertEqual(json.loads(response["body"]), {"status": "ok"})
        self.assertEqual(dict(response["headers"])["Cache-Control"], "no-store")

    def test_static_head_security_and_traversal(self):
        with patch.dict(os.environ, {"JOBNKILL_ENV": "production"}):
            response = self.request("/")
            head = self.request("/", "HEAD")
        self.assertEqual(response["body"], (WEB_ROOT / "index.html").read_bytes())
        self.assertEqual(head["body"], b"")
        headers = dict(head["headers"])
        self.assertEqual(int(headers["Content-Length"]), len(response["body"]))
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertIn("max-age=31536000", headers["Strict-Transport-Security"])
        self.assertEqual(self.request("/../requirements.txt")["status"], 404)
        self.assertEqual(self.request("/app.js")["body"], (WEB_ROOT / "app.js").read_bytes())

    def test_search_sources_and_unknown_routes(self):
        response = self.request("/api/jobs", extra={"QUERY_STRING": "q=test&limit=2&offset=0"})
        self.assertEqual(response["status"], 200)
        self.assertIn("items", json.loads(response["body"]))
        self.assertEqual(self.request("/api/sources")["status"], 200)
        self.assertEqual(self.request("/api/jobs", extra={"QUERY_STRING": "limit=bad"})["status"], 400)
        self.assertEqual(self.request("/api/unknown")["status"], 404)
        self.assertEqual(self.request("/api/health", "PATCH")["status"], 405)

    def test_declared_body_limit_is_global_and_rejected_without_read(self):
        class Unreadable:
            def read(self, size=-1):
                raise AssertionError("oversized request must not be read")
            def readline(self, size=-1):
                raise AssertionError("oversized request must not be read")
            def readlines(self, hint=-1):
                raise AssertionError("oversized request must not be read")
            def __iter__(self):
                return iter(())

        for method in ("GET", "POST", "PUT", "DELETE"):
            response = self.request(method=method, extra={
                "CONTENT_LENGTH": str(MAX_REQUEST_BYTES + 1), "wsgi.input": Unreadable(),
            })
            self.assertEqual(response["status"], 413)

    def test_production_compose_rejection_does_not_read_the_draft_body(self):
        class Unreadable:
            def read(self, size=-1):
                raise AssertionError("production browser-only compose body must not be read")
            def readline(self, size=-1):
                raise AssertionError("production browser-only compose body must not be read")
            def readlines(self, hint=-1):
                raise AssertionError("production browser-only compose body must not be read")
            def __iter__(self):
                return iter(())

        with patch.dict(os.environ, {"JOBNKILL_ENV": "production"}):
            response = self.request(
                "/api/drafts/compose",
                "POST",
                extra={"CONTENT_LENGTH": "64000", "wsgi.input": Unreadable()},
            )
        self.assertEqual(response["status"], 404)

    def test_chunked_body_limit_and_json(self):
        extra = {"CONTENT_LENGTH": "", "wsgi.input_terminated": True}
        response = self.request("/api/drafts/compose", "POST", b"x" * (MAX_REQUEST_BYTES + 1), extra)
        self.assertEqual(response["status"], 413)
        response = self.request("/api/drafts/compose", "POST", b"{}", extra)
        self.assertEqual(response["status"], 422)
        response = self.request("/api/drafts/compose", "POST", b"{}" + b" " * (MAX_REQUEST_BYTES - 2))
        self.assertEqual(response["status"], 422)

    def test_truncated_or_invalid_length_is_rejected(self):
        self.assertEqual(self.request(body=b"{}", extra={"CONTENT_LENGTH": "5"})["status"], 400)
        # Invalid Content-Length isn't a valid WSGI environ, so bypass validator.
        with patch("jobandkill.wsgi.initialize", return_value="unused"):
            self.app = create_app()
        for value in ("-1", "abc", "1,2"):
            self.assertEqual(self.request(extra={"CONTENT_LENGTH": value})["status"], 400)

    def test_access_logs_and_unhandled_errors_do_not_leak(self):
        output = StringIO()
        with (
            patch.dict(os.environ, {"JOBNKILL_QUIET": "0"}),
            patch("sys.stderr", output),
            patch("jobandkill.wsgi._WSGIHandler.do_GET", side_effect=RuntimeError("database-password-secret")),
        ):
            response = self.request("/login-token-secret", extra={
                "QUERY_STRING": "token=query-secret", "HTTP_COOKIE": "session=cookie-secret",
            })
        self.assertEqual(response["status"], 500)
        self.assertEqual(dict(response["headers"])["X-Content-Type-Options"], "nosniff")
        for secret in ("database-password-secret", "login-token-secret", "query-secret", "cookie-secret"):
            self.assertNotIn(secret, output.getvalue() + response["body"].decode())

    def test_startup_checks_production_settings_before_database(self):
        with (
            patch.dict(os.environ, {"JOBNKILL_ENV": "production"}),
            patch("jobandkill.wsgi.require_production_settings", side_effect=RuntimeError("configuration")) as check,
            patch("jobandkill.wsgi.initialize") as initialize,
        ):
            with self.assertRaisesRegex(RuntimeError, "configuration"):
                create_app()
        check.assert_called_once_with()
        initialize.assert_not_called()

    def test_blueprint_bounds_concurrency_timeouts_and_graceful_shutdown(self):
        root = Path(__file__).resolve().parents[1]
        blueprint = (root / "render.yaml").read_text()
        for setting in ("gunicorn 'jobandkill.wsgi:create_app()'", "--preload", "--workers 2", "--threads 1",
                        "--worker-class sync", "--timeout 30", "--graceful-timeout 25", "maxShutdownDelaySeconds: 30"):
            self.assertIn(setting, blueprint)
        self.assertNotIn("--access-logfile", blueprint)
        self.assertIn("--logger-class jobandkill.gunicorn_logging.RedactingLogger", blueprint)
        self.assertIn("gunicorn==26.2.0", (root / "requirements-production.txt").read_text())

    @unittest.skipUnless(importlib.util.find_spec("gunicorn"), "Gunicorn is an optional production dependency")
    def test_gunicorn_request_error_logs_redact_uri_and_exception(self):
        from jobandkill.gunicorn_logging import RequestErrorFilter

        for message, args in (("Error handling request %s %s", ("GET", "/?token=secret")),
                              ("Invalid request from ip=127.0.0.1: secret", ())):
            record = logging.LogRecord("gunicorn.error", logging.ERROR, __file__, 1, message, args,
                                       (ValueError, ValueError("secret"), None))
            self.assertTrue(RequestErrorFilter().filter(record))
            self.assertEqual(record.getMessage(), "http_server_request_error")
            self.assertIsNone(record.exc_info)

    @unittest.skipUnless(os.name == "posix" and importlib.util.find_spec("gunicorn"), "Gunicorn is an optional production dependency")
    def test_real_gunicorn_serves_requests_and_handles_sigterm(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(16)
            port = listener.getsockname()[1]
            child_environment = {**os.environ, "JOBNKILL_DATABASE_URL": "", "JOBNKILL_ENV": "test",
                                 "JOBNKILL_DB_PATH": str(Path(self.directory.name) / "gunicorn.db")}
            process = subprocess.Popen([
                sys.executable, "-m", "gunicorn", "jobandkill.wsgi:create_app()", "--preload",
                "--bind", f"fd://{listener.fileno()}", "--workers", "2", "--threads", "1",
                "--worker-class", "sync", "--timeout", "30", "--graceful-timeout", "25",
                "--logger-class", "jobandkill.gunicorn_logging.RedactingLogger",
            ], cwd=WEB_ROOT.parent, env=child_environment, pass_fds=(listener.fileno(),),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 10
            while True:
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1) as response:
                        self.assertEqual(json.load(response), {"status": "ok"})
                        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
                    break
                except OSError:
                    if time.monotonic() >= deadline or process.poll() is not None:
                        self.fail("Gunicorn did not become ready")
                    time.sleep(0.05)
            # Exercise Gunicorn's own chunk parser, not just a synthetic environ.
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/drafts/compose", data=iter([b"{}"]),
                headers={"Content-Type": "application/json"}, method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(request, timeout=2)
            self.assertEqual(raised.exception.code, 422)
            with socket.create_connection(("127.0.0.1", port), timeout=2) as malformed:
                malformed.sendall(b"GET /?token=uri-secret HTTP/1.1\r\nHost: localhost\r\nInvalid secret-header\r\n\r\n")
                self.assertIn(b"400", malformed.recv(4096))
            process.terminate()
            _, errors = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0)
            self.assertNotIn(b"uri-secret", errors)
            self.assertNotIn(b"secret-header", errors)
            self.assertIn(b"http_server_request_error", errors)
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
