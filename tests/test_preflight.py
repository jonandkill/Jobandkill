from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from jobandkill import preflight
from jobandkill.ingest import AlioApiSource, _RejectApiRedirect, normalize_api_record, parse_api_payload


# Deliberately synthetic offline fixtures. They are never sent to a service.
ENV = {
    "JOBNKILL_DATABASE_URL": "postgresql://test:sentinel-db@db.example.test/test?sslmode=require",
    "JOBNKILL_STORAGE_BACKEND": "s3",
    "JOBNKILL_S3_BUCKET": "test-bucket",
    "AWS_ACCESS_KEY_ID": "sentinel-access",
    "AWS_SECRET_ACCESS_KEY": "sentinel-secret",
    "JOBNKILL_ALIO_API_URL_TEMPLATE": "https://apis.data.go.kr/1051000/recruitment/list?serviceKey={service_key}&pageNo={page}&numOfRows={page_size}&resultType=json",
    "JOBNKILL_ALIO_SERVICE_KEY": "sentinel-api",
}


class PreflightTests(unittest.TestCase):
    def test_missing_configuration_never_calls_network(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(preflight, "_database_probe") as db, patch.object(preflight, "_storage_probe") as s3, patch.object(preflight, "_api_probe") as api:
            result = preflight.report(True)
        self.assertFalse(result["ready"])
        self.assertEqual(result["connection_checks"], "not_performed")
        for probe in (db, s3, api):
            probe.assert_not_called()
        for name in preflight.REQUIRED_SECRETS:
            self.assertFalse(next(c for c in result["checks"] if c["name"] == name)["ready"])

    def test_bad_templates_rejected_without_echo(self):
        good = ENV["JOBNKILL_ALIO_API_URL_TEMPLATE"]
        bad = [good.replace("https:", "http:"), good.replace("apis.data.go.kr", "data.go.kr.evil.test"),
               good + "&extra={unknown}", good.replace("{page}", "{page:02}"),
               good.replace("/list", "/..."), good.replace("apis.data.go.kr", "user:pass@apis.data.go.kr"),
               good + "#fragment", good + "{", good.replace("apis.data.go.kr", "[broken")]
        self.assertTrue(preflight.validate_api_template(good))
        for template in bad:
            with self.subTest(template=template):
                self.assertFalse(preflight.validate_api_template(template))

    def test_static_readiness_is_not_connection_validation(self):
        with patch.dict(os.environ, ENV, clear=True), patch("importlib.util.find_spec", return_value=object()):
            result = preflight.report()
        self.assertTrue(result["ready"])
        self.assertEqual(result["connection_checks"], "not_performed")

    def test_probe_failure_is_redacted_and_other_probes_still_run(self):
        with patch.dict(os.environ, ENV, clear=True), patch("importlib.util.find_spec", return_value=object()), patch.object(preflight, "_database_probe", side_effect=RuntimeError(ENV["JOBNKILL_DATABASE_URL"])), patch.object(preflight, "_storage_probe") as s3, patch.object(preflight, "_api_probe") as api:
            result = preflight.report(True)
        self.assertFalse(result["ready"])
        self.assertNotIn("sentinel", json.dumps(result))
        s3.assert_called_once()
        api.assert_called_once()

    def test_private_render_hostname_and_missing_tls_fail_before_connect(self):
        for url in ("postgresql://test:pass@dpg-private/test?sslmode=require", "postgresql://test:pass@db.example.test/test", "postgresql://test:pass@[bad/test?sslmode=require"):
            with patch.dict(os.environ, {**ENV, "JOBNKILL_DATABASE_URL": url}, clear=True), patch.object(preflight, "_database_probe") as db:
                result = preflight.report(True)
                self.assertFalse(result["configuration_ready"])
                db.assert_not_called()

    def test_summary_and_exit_code_on_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            summary = Path(tmp) / "summary.md"
            output = io.StringIO()
            with patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary)}, clear=True), redirect_stdout(output):
                code = preflight.main(["--summary", "--check-connections"])
            self.assertEqual(code, 2)
            self.assertIn("JOBNKILL_S3_ACCESS_KEY_ID", summary.read_text())
            self.assertIn("production", summary.read_text())

    def test_api_probe_uses_one_record_and_does_not_initialize_database(self):
        payload = b'{"response":{"header":{"resultCode":"00"},"body":{"items":[],"totalCount":0}}}'
        with patch.dict(os.environ, ENV, clear=True), patch("jobandkill.ingest._fetch_api_response", return_value=(payload, "application/json")) as fetch, patch("jobandkill.db.initialize") as init:
            preflight._api_probe()
            url = fetch.call_args.args[0].full_url
            self.assertIn("pageNo=1", url)
            self.assertIn("numOfRows=1", url)
            init.assert_not_called()


class ApiErrorTests(unittest.TestCase):
    def test_official_list_wrapper_and_non_authoritative_attachments(self):
        record = {"recrutPblntSn": 123, "instNm": "테스트 기관", "recrutPbancTtl": "테스트 공고", "files": [], "steps": []}
        payload = json.dumps({"resultCode": 0, "totalCount": 1, "result": [{"item": record}]}).encode()
        self.assertEqual(parse_api_payload(payload), ([record], 1))
        with patch.dict(os.environ, ENV, clear=True), patch("jobandkill.ingest._fetch_api_response", return_value=(payload, "application/json")):
            pages = list(AlioApiSource().fetch())
        posting = normalize_api_record(pages[0][0])
        self.assertEqual(posting.external_id, "123")
        self.assertFalse(posting.attachments_authoritative)

    def test_http_200_json_auth_error_is_not_empty_success(self):
        for code in ("20", "22", "30", "31", "sentinel-secret"):
            payload = json.dumps({"response": {"header": {"resultCode": code, "resultMsg": "sentinel-secret"}, "body": {"totalCount": 0, "items": []}}}).encode()
            with self.assertRaises(ValueError) as error:
                parse_api_payload(payload)
            self.assertNotIn("sentinel", str(error.exception))

    def test_xml_gateway_error_is_rejected(self):
        payload = b'<OpenAPI_ServiceResponse><cmmMsgHeader><errMsg>sentinel-secret</errMsg><returnReasonCode>30</returnReasonCode></cmmMsgHeader></OpenAPI_ServiceResponse>'
        with self.assertRaises(ValueError) as error:
            parse_api_payload(payload)
        self.assertNotIn("sentinel", str(error.exception))

    def test_unknown_empty_payload_does_not_claim_success(self):
        for payload in (b'{}', b'{"message":"sentinel-secret"}', b'[]', b'{"totalCount":-1}'):
            with self.assertRaises(ValueError):
                parse_api_payload(payload)

    def test_verified_empty_response_is_valid(self):
        self.assertEqual(parse_api_payload(b'{"response":{"header":{"resultCode":"00"},"body":{"items":[],"totalCount":0}}}'), ([], 0))

    def test_redirect_refuses_to_forward_service_key(self):
        with self.assertRaisesRegex(RuntimeError, "리다이렉트 차단"):
            _RejectApiRedirect().redirect_request(None, None, 302, "", {}, "https://evil.test/?key=sentinel-secret")

    def test_source_fetch_rejects_gateway_error_before_yielding(self):
        with patch.dict(os.environ, ENV, clear=True), patch("jobandkill.ingest._fetch_api_response", return_value=(b'{"response":{"header":{"resultCode":"30"}}}', "application/json")):
            with self.assertRaises(ValueError):
                list(AlioApiSource().fetch())


if __name__ == "__main__":
    unittest.main()
