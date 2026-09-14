"""Keep HTTP parser/timeout failures from exposing request secrets in logs."""
from __future__ import annotations

import logging

from gunicorn.glogging import Logger


class RequestErrorFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Gunicorn logs malformed headers and raw URIs in request-error messages
        # and traceback exception text. Keep severity/time, never those values.
        if "request" in str(record.msg).lower():
            record.msg = "http_server_request_error"
            record.args = ()
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        return True


class RedactingLogger(Logger):
    def setup(self, cfg) -> None:
        super().setup(cfg)
        self.error_log.addFilter(RequestErrorFilter())
