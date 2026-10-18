"""Safety regressions use synthetic credentials and isolated/stubbed services."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import traceback
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch

from jobandkill import preflight
from jobandkill.ingest import AlioApiSource, _fetch_api_response, normalize_api_record, parse_api_payload
from jobandkill.refresh_mode import resolve_mode
from test_preflight import ENV


RECORD = {"recrutPblntSn": 123, "instNm": "테스트 기관", "recrutPbancTtl": "공개 채용"}


class ConfigurationSafetyTests(unittest.TestCase):
    def test_database_query_cannot_override_validated_target_or_tls(self):
        base = ENV["JOBNKILL_DATABASE_URL"]
        for suffix in ("&sslmode=", "&sslmode=disable", "&host=127.0.0.1", "&hostaddr=127.0.0.1",
                       "&port=5433", "&dbname=other", "&service=other", "#fragment"):
            with self.subTest(suffix=suffix):
                self.assertFalse(preflight.validate_database_url(base + suffix))
        self.assertTrue(preflight.validate_database_url(base))
        self.assertTrue(preflight.validate_database_url(base.replace("require", "verify-full")))

    def test_private_ips_and_malformed_ports_are_rejected(self):
        for host in ("127.0.0.2", "10.1.2.3", "169.254.169.254", "db.localhost", "db.internal", "db.test:bad"):
            self.assertFalse(preflight.validate_database_url(ENV["JOBNKILL_DATABASE_URL"].replace("db.example.test", host)))

    def test_blank_or_duplicate_api_parameters_are_rejected(self):
        base = ENV["JOBNKILL_ALIO_API_URL_TEMPLATE"]
        for template in (base + "&serviceKey=", base + "&pageNo=", base.replace("resultType=json", "resultType=")):
            self.assertFalse(preflight.validate_api_template(template))

    def test_invalid_storage_config_never_calls_network(self):
        for changes in ({"JOBNKILL_S3_BUCKET": "s3://bucket"}, {"JOBNKILL_S3_BUCKET": "127.0.0.1"},
                        {"JOBNKILL_S3_BUCKET": "bad bucket"}, {"JOBNKILL_S3_ENDPOINT_URL": "https://s3.example.test:bad"},
                        {"JOBNKILL_S3_ENDPOINT_URL": "http://s3.example.test"}):
            with patch.dict(os.environ, {**ENV, **changes}, clear=True), patch.object(preflight, "_database_probe") as db, patch.object(preflight, "_storage_probe") as s3, patch.object(preflight, "_api_probe") as api:
                self.assertFalse(preflight.report(True)["configuration_ready"])
                for probe in (db, s3, api):
                    probe.assert_not_called()

    def test_temporary_aws_credentials_require_session_token(self):
        with patch.dict(os.environ, {**ENV, "AWS_ACCESS_KEY_ID": "ASIA_SYNTHETIC_TEST"}, clear=True):
            self.assertFalse(preflight.report()["configuration_ready"])
            os.environ["AWS_SESSION_TOKEN"] = "sentinel-session"
            self.assertTrue(preflight.report()["configuration_ready"])

    def test_static_and_connection_success_never_claim_collection(self):
        with patch.dict(os.environ, ENV, clear=True), patch.object(preflight, "_database_probe"), patch.object(preflight, "_storage_probe"), patch.object(preflight, "_api_probe"):
            static = preflight.report(False)
            online = preflight.report(True)
        self.assertFalse(static["read_only_diagnostics_passed"])
        self.assertTrue(online["read_only_diagnostics_passed"])
        self.assertFalse(online["collection_performed"])

    def test_all_probe_failures_and_summary_redact_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            summary = Path(tmp) / "summary.md"
            stdout = io.StringIO()
            bad = RuntimeError("sentinel-db sentinel-secret sentinel-api sentinel-session")
            with patch.dict(os.environ, {**ENV, "GITHUB_STEP_SUMMARY": str(summary)}, clear=True), patch.object(preflight, "_database_probe", side_effect=bad), patch.object(preflight, "_storage_probe", side_effect=bad), patch.object(preflight, "_api_probe", side_effect=bad), redirect_stdout(stdout):
                code = preflight.main(["--check-connections", "--summary"])
            self.assertEqual(code, 2)
            self.assertNotIn("sentinel", stdout.getvalue() + summary.read_text())


class ProbeSafetyTests(unittest.TestCase):
    def test_database_only_issues_read_only_queries_and_closes_connection(self):
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.execute.return_value.fetchone.side_effect = [("on",), (1,)]
        with patch.dict(os.environ, ENV, clear=True), patch("psycopg.connect", return_value=connection) as connect, patch("jobandkill.db.initialize") as init:
            preflight._database_probe()
        self.assertEqual([call.args[0] for call in connection.execute.call_args_list], ["SHOW transaction_read_only", "SELECT 1"])
        self.assertIn("default_transaction_read_only=on", connect.call_args.kwargs["options"])
        self.assertEqual(connect.call_args.kwargs["connect_timeout"], 10)
        connection.__exit__.assert_called_once()
        init.assert_not_called()

    def test_database_rejects_read_write_transaction(self):
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.execute.return_value.fetchone.return_value = ("off",)
        with patch.dict(os.environ, ENV, clear=True), patch("psycopg.connect", return_value=connection):
            with self.assertRaises(RuntimeError):
                preflight._database_probe()

    def test_s3_only_gets_versioning_and_rejects_versioned_buckets(self):
        import boto3
        from botocore.stub import Stubber
        for status in (None, "Enabled", "Suspended"):
            client = boto3.client("s3", region_name="ap-northeast-2", aws_access_key_id="synthetic", aws_secret_access_key="synthetic")
            with Stubber(client) as stub:
                stub.add_response("get_bucket_versioning", {} if status is None else {"Status": status}, {"Bucket": "test-bucket"})
                with patch.dict(os.environ, ENV, clear=True), patch("boto3.client", return_value=client) as factory:
                    if status is None:
                        preflight._storage_probe()
                    else:
                        with self.assertRaises(ValueError):
                            preflight._storage_probe()
                stub.assert_no_pending_responses()
                config = factory.call_args.kwargs["config"]
                self.assertEqual(config.connect_timeout, 10)
                self.assertEqual(config.read_timeout, 10)
                self.assertEqual(config.retries["total_max_attempts"], 2)
            client.close()

    def test_s3_denied_expired_and_missing_bucket_are_failures_without_remote_text(self):
        import boto3
        from botocore.stub import Stubber
        for code in ("AccessDenied", "ExpiredToken", "NoSuchBucket"):
            client = boto3.client("s3", region_name="ap-northeast-2", aws_access_key_id="synthetic", aws_secret_access_key="synthetic")
            with Stubber(client) as stub:
                stub.add_client_error("get_bucket_versioning", service_error_code=code, service_message="sentinel-secret", http_status_code=403)
                with patch.dict(os.environ, ENV, clear=True), patch("boto3.client", return_value=client), patch.object(preflight, "_database_probe"), patch.object(preflight, "_api_probe"):
                    result = preflight.report(True)
                self.assertFalse(result["read_only_diagnostics_passed"])
                self.assertNotIn("sentinel", json.dumps(result))
                stub.assert_no_pending_responses()
            client.close()

    def test_api_probe_rejects_more_than_requested_one_record(self):
        payload = json.dumps({"resultCode": 0, "totalCount": 2, "result": [{"item": RECORD}] * 2}).encode()
        with patch.dict(os.environ, ENV, clear=True), patch("jobandkill.ingest._fetch_api_response", return_value=(payload, "application/json")):
            with self.assertRaises(ValueError):
                preflight._api_probe()


class ApiContractSafetyTests(unittest.TestCase):
    def parse(self, payload):
        return parse_api_payload(json.dumps(payload).encode(), require_contract=True)

    def test_empty_response_requires_success_code_collection_and_valid_total(self):
        self.assertEqual(self.parse({"resultCode": 0, "totalCount": 0, "result": []}), ([], 0))
        for payload in ({"totalCount": 0}, {"totalCount": 0, "result": []},
                        {"resultCode": 0, "totalCount": 0}, {"resultCode": 0, "totalCount": 0, "result": ["sentinel"]},
                        {"resultCode": 0, "totalCount": 1, "result": []}):
            with self.assertRaises(ValueError):
                self.parse(payload)

    def test_non_integer_totals_and_contradictory_counts_are_rejected(self):
        for total in (False, 1.2, "1.2", -1, "sentinel", 0):
            with self.assertRaises(ValueError):
                self.parse({"resultCode": 0, "totalCount": total, "result": [{"item": RECORD}]})

    def test_nested_record_total_cannot_override_envelope_total(self):
        record = {**RECORD, "totalCount": 0}
        records, total = self.parse({"resultCode": 0, "totalCount": 100, "result": [{"item": record}]})
        self.assertEqual(total, 100)
        self.assertEqual(records, [record])

    def test_invalid_or_mixed_records_fail_before_yield(self):
        for record in ({"item": "sentinel"}, {"item": {"instNm": "a", "recrutPbancTtl": "b"}}, {"item": {**RECORD, "instNm": ""}}, "sentinel"):
            payload = json.dumps({"resultCode": 0, "totalCount": 2, "result": [{"item": RECORD}, record]}).encode()
            with patch.dict(os.environ, ENV, clear=True), patch("jobandkill.ingest._fetch_api_response", return_value=(payload, "application/json")):
                with self.assertRaises(ValueError):
                    next(AlioApiSource().fetch())

    def test_xml_single_multiple_and_empty_contract(self):
        item = '<item><recrutPblntSn>123</recrutPblntSn><instNm>Test</instNm><recrutPbancTtl>Hiring</recrutPbancTtl></item>'
        for count in (0, 1, 2):
            payload = f'<response><resultCode>0</resultCode><totalCount>{count}</totalCount><result>{item * count}</result></response>'.encode()
            records, total = parse_api_payload(payload, require_contract=True)
            self.assertEqual(len(records), count)
            self.assertEqual(total, count)

    def test_official_named_fields_take_precedence_over_codes(self):
        record = {**RECORD, "hireTypeNmLst": "정규직", "acbgCondNmLst": "학력무관", "workRgnNmLst": "울산,부산", "workRgnLst": "26,21", "ncsCdNmLst": "경영,회계", "ncsCdLst": "02,03"}
        posting = normalize_api_record(record)
        self.assertEqual(posting.employment_type, "정규직")
        self.assertEqual(posting.education, "학력무관")
        self.assertEqual(posting.regions, ["울산", "부산"])
        self.assertEqual(posting.ncs_categories, ["경영", "회계"])

    def test_http_and_transport_errors_do_not_leak_exception_chain(self):
        errors = [urllib.error.HTTPError("https://example.test/?key=sentinel", 401, "sentinel", {}, None),
                  urllib.error.URLError("sentinel"), TimeoutError("sentinel")]
        for error in errors:
            opener = MagicMock()
            opener.open.side_effect = error
            with patch("urllib.request.build_opener", return_value=opener), patch("jobandkill.ingest.time.sleep"):
                try:
                    _fetch_api_response(urllib.request.Request("https://example.test/"))
                except RuntimeError:
                    self.assertNotIn("sentinel", traceback.format_exc())
                else:
                    self.fail("request unexpectedly succeeded")
            self.assertEqual(opener.open.call_count, 1 if isinstance(error, urllib.error.HTTPError) else 3)

    def test_real_http_redirect_is_not_followed(self):
        paths = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                paths.append(self.path)
                self.send_response(302)
                self.send_header("Location", "/sink?key=sentinel")
                self.end_headers()
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/start")
            with self.assertRaisesRegex(RuntimeError, "리다이렉트 차단"):
                _fetch_api_response(request)
            self.assertEqual(paths, ["/start"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


class WorkflowIntentSafetyTests(unittest.TestCase):
    def test_missing_null_malformed_and_diagnostic_inputs_cannot_write(self):
        for value in (None, "", 0, [], {}, "FALSE", "true", True):
            self.assertFalse(resolve_mode("workflow_dispatch", {"diagnose_only": value, "full_sync": True})["write_enabled"])
        for inputs in (None, {}, [], "false"):
            self.assertFalse(resolve_mode("workflow_dispatch", inputs)["write_enabled"])

    def test_only_explicit_false_allows_manual_refresh(self):
        for value in (False, "false"):
            self.assertEqual(resolve_mode("workflow_dispatch", {"diagnose_only": value, "full_sync": "true"}), {"write_enabled": True, "full_sync": True})
        for event in ("pull_request", "push", "", "workflow_call"):
            self.assertFalse(resolve_mode(event, {"diagnose_only": False})["write_enabled"])
        self.assertEqual(resolve_mode("schedule", {}), {"write_enabled": True, "full_sync": False})

    def test_cli_with_missing_input_is_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            event, output = Path(tmp) / "event.json", Path(tmp) / "output"
            event.write_text('{"inputs": {"full_sync": true}}')
            result = subprocess.run([sys.executable, "-m", "jobandkill.refresh_mode"], env={**os.environ, "GITHUB_EVENT_PATH": str(event), "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_OUTPUT": str(output)}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(output.read_text(), "write_enabled=false\nfull_sync=false\n")
            event.write_text("invalid json")
            output.unlink()
            result = subprocess.run([sys.executable, "-m", "jobandkill.refresh_mode"], env={**os.environ, "GITHUB_EVENT_PATH": str(event), "GITHUB_EVENT_NAME": "schedule", "GITHUB_OUTPUT": str(output)}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(output.exists())

    def test_all_mutating_workflow_steps_require_explicit_mode_output(self):
        workflow = Path('.github/workflows/sync.yml').read_text()
        self.assertIn('run: python -m jobandkill.refresh_mode', workflow)
        checked = 0
        for block in workflow.split('      - name:'):
            if any(command in block for command in ('run: python -m jobandkill init', 'run: python -m jobandkill sync', 'run: python -m jobandkill process-documents')):
                self.assertIn("if: ${{ steps.mode.outputs.write_enabled == 'true'", block)
                self.assertNotIn('continue-on-error', block)
                checked += 1
        self.assertEqual(checked, 4)
        self.assertIn('timeout-minutes: 5', workflow)


if __name__ == '__main__':
    unittest.main()
