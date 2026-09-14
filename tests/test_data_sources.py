from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jobandkill.__main__ import main
from jobandkill.data_sources import data_coverage, source_inventory
from jobandkill.db import connect, initialize


class DataCoverageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "catalog.db"
        initialize(self.path)

    def test_empty_database_does_not_claim_complete_or_absent_government_data(self):
        with connect(self.path) as connection:
            report = data_coverage(connection)
        self.assertFalse(report["all_government_data_complete"])
        self.assertEqual(report["scope"], "registered_sources_only")
        self.assertEqual(report["documents"]["total"], 0)
        self.assertFalse(report["ai_policy"]["official_data_completion_by_ai"])
        json.dumps(report, ensure_ascii=False)

    def test_source_rights_and_missing_fields_are_explicit(self):
        sources = {source["slug"]: source for source in source_inventory()}
        self.assertEqual(sources["worknet-dictionary"]["rights"], "restricted")
        self.assertEqual(sources["know-research"]["rights"], "restricted")
        self.assertEqual(sources["ncs-training-2025"]["rights"], "kogl_type_1_attribution")
        self.assertIn("knowledge", sources["ncs-common"]["not_provided"])
        self.assertEqual(sources["ncs-reference"]["implementation"], "spec_verified")
        sources["ncs-common"]["fields"].append("invented")
        self.assertNotIn("invented", source_inventory()[0]["fields"])

    @patch("jobandkill.__main__._print")
    @patch("jobandkill.__main__.sync_ncs_catalog")
    def test_catalog_sync_exit_status_separates_partial(self, sync, output):
        sync.return_value = {"status": "partial"}
        self.assertEqual(main(["catalog-sync", "--max-pages", "2"]), 3)
        sync.assert_called_once_with(None, max_pages=2, restart=False)
        sync.return_value = {"status": "completed"}
        self.assertEqual(main(["catalog-sync", "--restart"]), 0)

    @patch("jobandkill.__main__._print")
    @patch("jobandkill.__main__.import_ncs_training_csv")
    def test_csv_import_is_not_mislabeled_api_full_sync(self, importer, output):
        importer.return_value = {"status": "imported"}
        self.assertEqual(main(["catalog-import", "--format", "training-csv", "official.csv"]), 0)
        importer.assert_called_once_with(Path("official.csv"), None)


if __name__ == "__main__":
    unittest.main()
