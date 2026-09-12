"""Contract fixtures based on published fields, not captured live API responses."""
from __future__ import annotations

import json
import hashlib
import os
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

from jobandkill.catalog import (
    CatalogError, NCS_ENDPOINT, NCS_SOURCE, PAGE_SIZE, _fetch_page, _NoRedirect,
    catalog_coverage, get_catalog_item, import_ncs_catalog, list_catalog,
    parse_ncs_payload, sync_ncs_catalog, import_ncs_training_csv,
)
from jobandkill.db import connect, initialize
from jobandkill.ingest import ConfigurationError


def record(number: int = 1, version: str = "24v1") -> dict[str, str]:
    return {
        "ncsClCd": f"{number:010d}_{version}", "compeUnitName": f"사무행정 {number}",
        "compeUnitLevel": "3", "ncsLclasCdnm": "경영·회계·사무",
        "ncsMclasCdnm": "총무·인사", "ncsSclasCdnm": "일반사무", "ncsSubdCdnm": "사무행정",
        "compeUnitDef": "업무 자료를 수집하고 검증하여 문서를 작성하는 능력이다.",
        "ncsLastLinkDt": "20260901",
    }


def payload(items: list[dict] | None = None, total: int = 1, page: int = 1, size: int = PAGE_SIZE) -> bytes:
    return json.dumps({"response": {"header": {"resultCode": "00", "resultMsg": "NORMAL SERVICE"},
        "body": {"items": {"item": [record()] if items is None else items}, "totalCount": total,
                 "pageNo": page, "numOfRows": size}}}, ensure_ascii=False).encode()


class CatalogParserTests(unittest.TestCase):
    def test_json_contract_preserves_version_and_official_fields(self) -> None:
        result = parse_ncs_payload(payload())
        self.assertEqual(result["items"][0]["ncsClCd"], "0000000001_24v1")
        self.assertEqual(result["reported_total"], 1)

    def test_single_json_item_and_unwrapped_header_body(self) -> None:
        data = json.loads(payload())["response"]
        data["body"]["items"]["item"] = record()
        result = parse_ncs_payload(json.dumps(data).encode())
        self.assertEqual(len(result["items"]), 1)

    def test_namespaced_xml_contract(self) -> None:
        xml = b'''<response xmlns="urn:fixture"><header><resultCode>00</resultCode></header>
          <body><items><item><ncsClCd>0000000001_24v1</ncsClCd>
          <compeUnitName>Fixture</compeUnitName></item></items><totalCount>1</totalCount>
          <pageNo>1</pageNo><numOfRows>100</numOfRows></body></response>'''
        self.assertEqual(parse_ncs_payload(xml)["items"][0]["compeUnitName"], "Fixture")

    def test_empty_successful_page_is_valid_only_as_zero_records(self) -> None:
        self.assertEqual(parse_ncs_payload(payload([], total=0))["items"], [])

    def test_api_error_never_exposes_upstream_message(self) -> None:
        secret = "SHOULD-NOT-LEAK"
        data = json.loads(payload())
        data["response"]["header"] = {"resultCode": "30", "resultMsg": secret}
        with self.assertRaises(CatalogError) as caught:
            parse_ncs_payload(json.dumps(data).encode())
        self.assertEqual(str(caught.exception), "upstream_api_error")
        self.assertNotIn(secret, str(caught.exception))

    def test_malformed_payloads_are_rejected(self) -> None:
        for raw in (b"", b"<broken", b"{broken", b"null", b"[]", b"{}", b"\xff"):
            with self.subTest(raw=raw), self.assertRaises(CatalogError):
                parse_ncs_payload(raw)

    def test_entities_are_rejected(self) -> None:
        raw = b'<!DOCTYPE response [<!ENTITY secret "private">]><response>&secret;</response>'
        with self.assertRaisesRegex(CatalogError, "unsafe_xml"):
            parse_ncs_payload(raw)

    def test_deep_xml_rejected_before_recursive_mapping(self) -> None:
        with self.assertRaises(CatalogError):
            parse_ncs_payload(b"<a>" * 20 + b"ok" + b"</a>" * 20)

    def test_negative_missing_and_boolean_total_are_rejected(self) -> None:
        for invalid in (-1, True, None, "1.5", "99999999999"):
            data = json.loads(payload())
            data["response"]["body"]["totalCount"] = invalid
            with self.subTest(invalid=invalid), self.assertRaises(CatalogError):
                parse_ncs_payload(json.dumps(data).encode())

    def test_oversized_and_nested_record_lists_rejected(self) -> None:
        with self.assertRaises(CatalogError):
            parse_ncs_payload(payload([record(), record(2)], size=1, total=2))
        with self.assertRaises(CatalogError):
            parse_ncs_payload(payload([[]]))


class CatalogNetworkTests(unittest.TestCase):
    def fake_opener(self, raw: bytes, response_url: str | None = None) -> MagicMock:
        opener = MagicMock()
        def open_response(request: urllib.request.Request, timeout: int) -> MagicMock:
            response = MagicMock()
            response.getcode.return_value = 200
            response.geturl.return_value = response_url or request.full_url
            response.read.return_value = raw
            response.__enter__.return_value = response
            return response
        opener.open.side_effect = open_response
        return opener

    def test_only_fixed_https_endpoint_and_bounded_read(self) -> None:
        opener = self.fake_opener(payload())
        with patch("jobandkill.catalog.urllib.request.build_opener", return_value=opener) as build:
            self.assertEqual(_fetch_page("fixture-key/+", 1)["reported_total"], 1)
        request = opener.open.call_args.args[0]
        parsed = urllib.parse.urlsplit(request.full_url)
        self.assertEqual(urllib.parse.urlunsplit(parsed._replace(query="")), NCS_ENDPOINT)
        self.assertEqual(urllib.parse.parse_qs(parsed.query)["serviceKey"], ["fixture-key/+"])
        self.assertIsInstance(build.call_args.args[0], _NoRedirect)
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 30)

    def test_redirect_handler_never_forwards_key(self) -> None:
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.test"))

    def test_unexpected_response_url_is_blocked(self) -> None:
        with patch("jobandkill.catalog.urllib.request.build_opener", return_value=self.fake_opener(payload(), "https://evil.test")):
            with self.assertRaisesRegex(CatalogError, "upstream_redirect_blocked"):
                _fetch_page("fixture-key", 1)

    def test_error_with_secret_url_is_sanitized(self) -> None:
        opener = MagicMock()
        opener.open.side_effect = urllib.error.URLError("https://x.test?serviceKey=SUPERSECRET")
        with patch("jobandkill.catalog.urllib.request.build_opener", return_value=opener):
            with self.assertRaisesRegex(CatalogError, "^upstream_fetch_failed$"):
                _fetch_page("SUPERSECRET", 1)

    def test_reflected_key_is_never_ingested(self) -> None:
        data = record()
        data["compeUnitDef"] = "private-service-key"
        with patch("jobandkill.catalog.urllib.request.build_opener", return_value=self.fake_opener(payload([data]))):
            with self.assertRaisesRegex(CatalogError, "credential_reflection_blocked"):
                _fetch_page("private-service-key", 1)

    def test_wrong_page_is_rejected(self) -> None:
        with patch("jobandkill.catalog.urllib.request.build_opener", return_value=self.fake_opener(payload(page=2))):
            with self.assertRaisesRegex(CatalogError, "unexpected_page"):
                _fetch_page("fixture-key", 1)


class CatalogDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "catalog.db"
        self.environment = patch.dict(os.environ, {"JOBNKILL_ENV": "test", "JOBNKILL_NCS_SERVICE_KEY": "fixture-service-key"}, clear=True)
        self.environment.start()
        initialize(self.path)

    def tearDown(self) -> None:
        self.environment.stop()
        self.directory.cleanup()

    def import_response(self, raw: bytes | None = None) -> dict:
        fixture = Path(self.directory.name) / "response.json"
        fixture.write_bytes(payload() if raw is None else raw)
        return import_ncs_catalog(fixture, self.path)

    def test_import_is_idempotent_separate_from_postings_and_never_completed(self) -> None:
        first = self.import_response()
        second = self.import_response()
        self.assertEqual(first["status"], "imported")
        self.assertEqual(second["status"], "imported")
        with connect(self.path) as connection:
            records = list_catalog(connection, "사무행정")
            self.assertEqual(len(records), 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM postings").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM institutions").fetchone()[0], 0)
            detail = get_catalog_item(connection, records[0]["id"])
        self.assertEqual(detail["version"], "24v1")
        self.assertEqual(detail["standard_code"], "0000000001")
        self.assertEqual(detail["raw"]["ncsClCd"], "0000000001_24v1")
        self.assertEqual(detail["knowledge"], [])
        self.assertIn("performance_criteria", detail["missing_fields"])

    def test_versions_do_not_overwrite_each_other(self) -> None:
        self.import_response(payload([record(1, "24v1"), record(1, "25v2")], total=2))
        with connect(self.path) as connection:
            self.assertEqual({item["version"] for item in list_catalog(connection)}, {"24v1", "25v2"})

    def test_raw_unknown_fields_cannot_store_reflected_credentials(self) -> None:
        item = {**record(), "serviceKey": "SECRET", "secretDebugUrl": "SECRET"}
        self.import_response(payload([item]))
        with connect(self.path) as connection:
            row = connection.execute("SELECT raw_json FROM occupation_catalog").fetchone()
        self.assertNotIn("SECRET", row["raw_json"])

    def test_missing_key_does_not_create_a_sync_run(self) -> None:
        with patch.dict(os.environ, {"JOBNKILL_NCS_SERVICE_KEY": ""}), self.assertRaises(ConfigurationError):
            sync_ncs_catalog(self.path)
        with connect(self.path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM catalog_sync_runs").fetchone()[0], 0)

    def test_page_budget_and_resume_only_fetch_next_page(self) -> None:
        first_page = parse_ncs_payload(payload([record(i) for i in range(100)], total=101))
        last_page = parse_ncs_payload(payload([record(100)], total=101, page=2))
        with patch("jobandkill.catalog._fetch_page", side_effect=[first_page, last_page]) as fetch:
            partial = sync_ncs_catalog(self.path, max_pages=1)
            completed = sync_ncs_catalog(self.path, max_pages=1)
        self.assertEqual(partial["status"], "partial")
        self.assertEqual(partial["unique_count"], 100)
        self.assertEqual(partial["next_page"], 2)
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["unique_count"], 101)
        self.assertEqual(completed["run_id"], partial["run_id"])
        self.assertEqual([call.args[1] for call in fetch.call_args_list], [1, 2])

    def test_restart_uses_new_run_and_first_page_without_deleting_catalog(self) -> None:
        page = parse_ncs_payload(payload([record(i) for i in range(100)], total=200))
        with patch("jobandkill.catalog._fetch_page", return_value=page) as fetch:
            first = sync_ncs_catalog(self.path, max_pages=1)
            restarted = sync_ncs_catalog(self.path, max_pages=1, restart=True)
        self.assertNotEqual(first["run_id"], restarted["run_id"])
        self.assertEqual(restarted["unique_count"], 100)
        self.assertEqual([call.args[1] for call in fetch.call_args_list], [1, 1])
        with connect(self.path) as connection:
            self.assertEqual(catalog_coverage(connection)["catalog_records"], 100)

    def test_repeated_page_stops_without_checkpoint_advance(self) -> None:
        page = parse_ncs_payload(payload([record(i) for i in range(100)], total=200))
        with patch("jobandkill.catalog._fetch_page", side_effect=[page, {**page, "page_no": 2}]) as fetch:
            result = sync_ncs_catalog(self.path)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["next_page"], 2)
        self.assertEqual(result["unique_count"], 100)
        self.assertEqual(result["error_summary"], "repeated_page_restart_required")
        with patch("jobandkill.catalog._fetch_page") as fetch:
            again = sync_ncs_catalog(self.path)
        self.assertEqual(again["run_id"], result["run_id"])
        fetch.assert_not_called()

    def test_duplicate_count_cannot_claim_completion(self) -> None:
        page = parse_ncs_payload(payload([record(), record()], total=2))
        with patch("jobandkill.catalog._fetch_page", return_value=page):
            result = sync_ncs_catalog(self.path)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["unique_count"], 1)
        self.assertEqual(result["duplicates"], 1)

    def test_changing_total_requires_restart(self) -> None:
        first = parse_ncs_payload(payload([record(i) for i in range(100)], total=101))
        second = parse_ncs_payload(payload([record(100)], total=102, page=2))
        with patch("jobandkill.catalog._fetch_page", side_effect=[first, second]):
            result = sync_ncs_catalog(self.path)
        self.assertEqual(result["error_summary"], "total_changed_restart_required")
        self.assertEqual(result["next_page"], 2)

    def test_invalid_record_rolls_back_whole_page(self) -> None:
        page = parse_ncs_payload(payload([record(), {"ncsClCd": "bad"}], total=2))
        with patch("jobandkill.catalog._fetch_page", return_value=page):
            result = sync_ncs_catalog(self.path)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["next_page"], 1)
        with connect(self.path) as connection:
            self.assertEqual(catalog_coverage(connection)["catalog_records"], 0)

    def test_transient_error_is_redacted_and_retry_completes(self) -> None:
        with patch("jobandkill.catalog._fetch_page", side_effect=RuntimeError("SERVICE-KEY-SECRET")):
            failed = sync_ncs_catalog(self.path)
        self.assertNotIn("SECRET", json.dumps(failed))
        with patch("jobandkill.catalog._fetch_page", return_value=parse_ncs_payload(payload())):
            completed = sync_ncs_catalog(self.path)
        self.assertEqual(completed["run_id"], failed["run_id"])
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["error_summary"], "")

    def test_short_or_empty_page_cannot_claim_completion(self) -> None:
        for items in ([record()], []):
            with self.subTest(items=items), patch("jobandkill.catalog._fetch_page", return_value=parse_ncs_payload(payload(items, total=200))):
                result = sync_ncs_catalog(self.path, restart=True)
            self.assertNotEqual(result["status"], "completed")

    def test_zero_count_is_valid_api_scope_completion(self) -> None:
        with patch("jobandkill.catalog._fetch_page", return_value=parse_ncs_payload(payload([], total=0))):
            result = sync_ncs_catalog(self.path)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["unique_count"], 0)

    def test_offline_import_does_not_move_or_complete_api_checkpoint(self) -> None:
        page = parse_ncs_payload(payload([record(i) for i in range(100)], total=101))
        with patch("jobandkill.catalog._fetch_page", return_value=page):
            partial = sync_ncs_catalog(self.path, max_pages=1)
        self.import_response(payload([record(100)], total=101, page=2))
        with connect(self.path) as connection:
            source = catalog_coverage(connection)["sources"][0]
        self.assertEqual(source["records"], 101)
        self.assertEqual(source["latest_run"]["status"], "imported")
        self.assertEqual(source["latest_api_run"]["status"], "partial")
        self.assertEqual(source["latest_api_run"]["next_page"], partial["next_page"])

    def test_search_bounds_and_literal_wildcards(self) -> None:
        self.import_response()
        with connect(self.path) as connection:
            self.assertEqual(list_catalog(connection, "%"), [])
            self.assertEqual(list_catalog(connection, "__"), [])
            self.assertEqual(list_catalog(connection, "\" OR 1=1 --"), [])
            self.assertIsNone(get_catalog_item(connection, "bad-id"))
            for args in ({"query": "x" * 201}, {"limit": 101}, {"limit": True}, {"offset": -1}):
                with self.subTest(args=args), self.assertRaises(ValueError):
                    list_catalog(connection, **args)

    def test_max_pages_is_integer_bounded(self) -> None:
        for value in (0, -1, 10_001, True, 1.5, "2"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                sync_ncs_catalog(self.path, max_pages=value)

    def test_coverage_never_presents_ksa_as_available(self) -> None:
        self.import_response()
        with connect(self.path) as connection:
            coverage = catalog_coverage(connection)
        self.assertEqual(coverage["catalog_records"], 1)
        self.assertEqual(coverage["missing_fields"]["knowledge"], 1)
        self.assertFalse(coverage["all_government_data_collected"])

    def training_csv(self, text: str, encoding: str = "cp949") -> Path:
        path = Path(self.directory.name) / "training.csv"
        path.write_bytes(text.encode(encoding))
        return path

    def test_training_cp949_official_blank_value_is_preserved_as_missing(self) -> None:
        # Actual row from 15083321: do not invent training hours for missing cells.
        text = "분류번호,명칭,수준,훈련시간\r\n0403020314_16v1,이러닝운영 지원도구관리,4,\r\n"
        path = self.training_csv(text)
        result = import_ncs_training_csv(path, self.path)
        self.assertEqual(result["unique_count"], 1)
        self.assertEqual(result["status"], "imported")
        self.assertEqual(result["license_code"], "KOGL1")
        self.assertEqual(result["source_file_hash"], hashlib.sha256(text.encode("cp949")).hexdigest())
        with connect(self.path) as connection:
            item = list_catalog(connection)[0]
            detail = get_catalog_item(connection, item["id"])
            coverage = catalog_coverage(connection)
        self.assertEqual(item["training_hours"], "")
        self.assertIn("training_hours", item["missing_fields"])
        self.assertEqual(detail["raw"]["훈련시간"], "")
        self.assertEqual(coverage["missing_fields"]["training_hours"], 1)
        self.assertEqual(item["summary"], "")
        self.assertIn("summary", item["missing_fields"])
        self.assertEqual(item["provider"], "한국산업인력공단")

    def test_training_quoted_comma_and_utf8_bom(self) -> None:
        text = '분류번호,명칭,수준,훈련시간\n0101010101_17v2,"기획, 검토",7,80\n'
        result = import_ncs_training_csv(self.training_csv(text, "utf-8-sig"), self.path)
        self.assertEqual(result["unique_count"], 1)
        with connect(self.path) as connection:
            item = list_catalog(connection)[0]
        self.assertEqual(item["job_title"], "기획, 검토")
        self.assertEqual(item["training_hours"], "80")

    def test_training_sources_do_not_overwrite_qnet_records(self) -> None:
        self.import_response()
        text = '분류번호,명칭,수준,훈련시간\n0000000001_24v1,사무행정 1,3,30\n'
        path = self.training_csv(text)
        import_ncs_training_csv(path, self.path)
        import_ncs_training_csv(path, self.path)
        with connect(self.path) as connection:
            items = list_catalog(connection)
        self.assertEqual(len(items), 2)
        self.assertEqual({item["source_slug"] for item in items}, {"ncs-common", "ncs-training-2025"})
        self.assertEqual(len({item["id"] for item in items}), 2)

    def test_training_unknown_columns_are_rejected(self) -> None:
        path = self.training_csv("분류번호,명칭,수준,비밀번호\n0000000001_24v1,사무,3,secret\n")
        with self.assertRaisesRegex(CatalogError, "unexpected_training_csv_columns"):
            import_ncs_training_csv(path, self.path)

    def test_training_bad_later_row_does_not_partially_import(self) -> None:
        path = self.training_csv("분류번호,명칭,수준,훈련시간\n0000000001_24v1,사무,3,30\nBAD,오류,3,30\n")
        with self.assertRaises(CatalogError):
            import_ncs_training_csv(path, self.path)
        with connect(self.path) as connection:
            self.assertEqual(catalog_coverage(connection)["catalog_records"], 0)

    def test_training_empty_file_and_malformed_quote_are_rejected(self) -> None:
        for text in ("분류번호,명칭,수준,훈련시간\n", '분류번호,명칭,수준,훈련시간\n"broken'):
            with self.subTest(text=text), self.assertRaises(CatalogError):
                import_ncs_training_csv(self.training_csv(text), self.path)

    def test_training_truncated_row_is_not_an_explicit_empty_cell(self) -> None:
        text = "분류번호,명칭,수준,훈련시간\n0000000001_24v1,사무\n"
        with self.assertRaisesRegex(CatalogError, "invalid_training_csv_row"):
            import_ncs_training_csv(self.training_csv(text), self.path)
        with connect(self.path) as connection:
            self.assertEqual(catalog_coverage(connection)["catalog_records"], 0)

    def test_training_malformed_header_quote_has_safe_error(self) -> None:
        with self.assertRaisesRegex(CatalogError, "invalid_training_csv"):
            import_ncs_training_csv(self.training_csv('"broken'), self.path)


if __name__ == "__main__":
    unittest.main()
