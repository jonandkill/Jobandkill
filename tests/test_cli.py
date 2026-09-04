from __future__ import annotations

import unittest
from unittest.mock import patch

from jobandkill.__main__ import main


class ProcessDocumentsCliTests(unittest.TestCase):
    @patch("jobandkill.__main__._print")
    @patch("jobandkill.__main__.process_documents")
    def test_fail_on_error_returns_nonzero_when_a_document_fails(self, process_documents, _print) -> None:
        process_documents.return_value = {"seen": 2, "parsed": 1, "unsupported": 0, "failed": 1}

        self.assertEqual(main(["process-documents", "--fail-on-error"]), 4)

    @patch("jobandkill.__main__._print")
    @patch("jobandkill.__main__.process_documents")
    def test_fail_on_error_succeeds_when_no_document_fails(self, process_documents, _print) -> None:
        process_documents.return_value = {
            "seen": 1, "parsed": 1, "unsupported": 0, "failed": 0, "gc_failed": 0,
        }

        self.assertEqual(main(["process-documents", "--fail-on-error"]), 0)

    @patch("jobandkill.__main__._print")
    @patch("jobandkill.__main__.process_documents")
    def test_fail_on_error_returns_nonzero_when_storage_cleanup_fails(
        self, process_documents, _print
    ) -> None:
        process_documents.return_value = {
            "seen": 0, "parsed": 0, "unsupported": 0, "failed": 0, "gc_failed": 1,
        }

        self.assertEqual(main(["process-documents", "--fail-on-error"]), 4)

    @patch("jobandkill.__main__._print")
    @patch("jobandkill.__main__.process_documents")
    def test_fail_on_error_returns_nonzero_for_quarantined_legacy_object(
        self, process_documents, _print
    ) -> None:
        process_documents.return_value = {
            "seen": 0, "parsed": 0, "unsupported": 0, "failed": 0,
            "gc_failed": 0, "quarantined": 1,
        }

        self.assertEqual(main(["process-documents", "--fail-on-error"]), 4)

    @patch("jobandkill.__main__._print")
    @patch("jobandkill.__main__.process_documents")
    def test_fail_on_error_returns_nonzero_for_due_gc_backlog(
        self, process_documents, _print
    ) -> None:
        process_documents.return_value = {
            "seen": 0, "parsed": 0, "unsupported": 0, "failed": 0,
            "gc_failed": 0, "gc_pending": 1, "quarantined": 0,
        }

        self.assertEqual(main(["process-documents", "--fail-on-error"]), 4)


class SyncCliTests(unittest.TestCase):
    @patch("jobandkill.__main__._print")
    @patch("jobandkill.__main__.sync_source")
    def test_partial_sync_returns_nonzero(self, sync_source, _print) -> None:
        sync_source.return_value = {"source": "data-go-kr-alio", "status": "partial"}

        self.assertEqual(main(["sync", "--source", "data-go-kr-alio"]), 3)


if __name__ == "__main__":
    unittest.main()
