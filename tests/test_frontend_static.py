from __future__ import annotations

import unittest
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class MarkupScan(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []
        self.inline_code: list[tuple[str, str]] = []
        self.buttons_without_type: list[str] = []
        self.dialogs_without_label: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if values.get("id"):
            self.ids.append(str(values["id"]))
        for name in values:
            if name == "style" or name.startswith("on"):
                self.inline_code.append((tag, name))
        if tag == "button" and values.get("type") != "button" and values.get("type") != "submit":
            self.buttons_without_type.append(str(values.get("id", "unnamed")))
        if tag == "dialog" and not values.get("aria-labelledby"):
            self.dialogs_without_label.append(str(values.get("id", "unnamed")))


class FrontendStaticTests(unittest.TestCase):
    def test_standard_catalog_is_separate_from_institution_jobs(self) -> None:
        markup = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        for required in (
            'name="archive-scope" value="catalog" checked',
            'name="archive-scope" value="jobs"',
            'id="catalog-source-state"', 'id="catalog-detail"',
            "NCS 직무능력 참고", "기관별 채용 직무",
            "미수집·인증 대기는 정부에 자료가 없다는 뜻이 아닙니다",
        ):
            self.assertIn(required, markup)
        self.assertIn('api("/api/data-coverage")', javascript)
        self.assertIn("정부 전체 자료의 총수가 아닙니다", javascript)
        self.assertIn("이 정보원에서 제공하지 않는 항목", javascript)
        catalog = javascript.split('let archiveScope = "catalog";', 1)[1].split("async function searchJobs(", 1)[0]
        self.assertNotIn(".innerHTML", catalog)
        self.assertNotIn("item.raw", catalog)
        self.assertNotIn("state.institution =", catalog)
        self.assertIn("비어 있는 항목에 참고 자료 적용", catalog)

    def test_markup_supports_strict_content_security_policy(self) -> None:
        scan = MarkupScan()
        scan.feed((ROOT / "web" / "index.html").read_text(encoding="utf-8"))
        duplicates = {item for item in scan.ids if scan.ids.count(item) > 1}
        self.assertEqual(duplicates, set())
        self.assertEqual(scan.inline_code, [])
        self.assertEqual(scan.buttons_without_type, [])
        self.assertEqual(scan.dialogs_without_label, [])

    def test_mobile_breakpoints_and_reduced_motion_are_present(self) -> None:
        css = (ROOT / "web" / "styles.css").read_text(encoding="utf-8")
        for rule in ("max-width: 900px", "max-width: 640px", "max-width: 360px", "prefers-reduced-motion"):
            self.assertIn(rule, css)
        self.assertIn("min-height: 44px", css)
        self.assertIn(".builder-nav .autosave-state", css)
        self.assertNotIn(".autosave-state { display: none; }", css)

    def test_login_and_sync_controls_are_accessible_without_blocking_builder(self) -> None:
        markup = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        for required in (
            'id="open-auth"', 'id="auth-dialog"', 'id="sync-notice"',
            'id="autosave-state" role="status"', 'id="builder-title" tabindex="-1"',
            'id="remember-draft"', 'id="draft-storage-status"', 'id="sensitive-data-warning"',
            'id="input-length-warning"',
        ):
            self.assertIn(required, markup)

    def test_login_has_version_bound_privacy_and_separate_overseas_consent_controls(self) -> None:
        markup = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        for required in (
            'id="privacy-policy-accepted"', 'id="privacy-policy-link"',
            'id="overseas-transfer-accepted"', '국외 이전 안내',
        ):
            self.assertIn(required, markup)
        self.assertIn('api("/api/privacy-config")', javascript)
        self.assertIn("privacy_policy_version", javascript)
        self.assertIn("overseas_transfer_version", javascript)

    def test_javascript_never_sends_drafts_outside_same_origin(self) -> None:
        javascript = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        self.assertNotIn("http://", javascript)
        self.assertNotIn("https://", javascript)
        self.assertNotIn('fetch("/api/drafts/compose"', javascript)
        self.assertIn("JobAndKillComposer.compose(state)", javascript)
        self.assertIn("history.replaceState", javascript)
        self.assertNotIn('localStorage.setItem("login_token"', javascript)

    def test_account_sync_is_explicit_and_scoped_to_the_client_key(self) -> None:
        javascript = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn("pendingMode: null", javascript)
        self.assertIn("clientKey: getClientKey()", javascript)
        self.assertIn("item.client_key === localClientKey", javascript)
        self.assertNotIn("|| listing.items[0]", javascript)
        self.assertIn("client_key: syncState.clientKey", javascript)
        self.assertIn("syncState.clientKey = serverDraft.client_key", javascript)
        self.assertIn('pendingMode === "conflict"', javascript)
        self.assertIn('showSyncNotice("load", serverDraft)', javascript)
        reconciliation = javascript.split("async function reconcileDrafts()", 1)[1].split("async function loadAuth()", 1)[0]
        self.assertNotIn("syncState.consent = true", reconciliation)
        self.assertNotIn("hydrateFromServer(", reconciliation)
        keep_local = javascript.split("async function keepLocalDraft()", 1)[1].split("function useServerDraft()", 1)[0]
        for reset in ("syncState.draftId = null", "syncState.revision = null", "syncState.clientKey = getClientKey()"):
            self.assertIn(reset, keep_local)

    def test_browser_draft_retention_is_session_scoped_and_explicit(self) -> None:
        javascript = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        markup = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        css = (ROOT / "web" / "styles.css").read_text(encoding="utf-8")
        self.assertIn("const DRAFT_TTL_MS = 30 * 24 * 60 * 60 * 1000", javascript)
        self.assertIn('storageSet("session", SESSION_DRAFT_KEY', javascript)
        self.assertIn("expiresAt: Date.now() + DRAFT_TTL_MS", javascript)
        self.assertIn('storageRemove("local", REMEMBERED_DRAFT_KEY)', javascript)
        self.assertIn("removeLegacyDraftStorage();", javascript)
        self.assertNotIn("localStorage.getItem(LEGACY_STORAGE_KEY)", javascript)
        self.assertIn("주민등록번호, 계좌번호, 건강·장애", markup)
        self.assertIn("공용·공개 기기에서는 30일 보관을 사용하지 마세요", markup)
        self.assertIn("다음 방문 시 삭제됩니다", markup)
        self.assertIn(".draft-privacy", css)

    def test_browser_storage_failures_do_not_break_draft_editing(self) -> None:
        javascript = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn("function storageGet(", javascript)
        self.assertIn("function storageSet(", javascript)
        self.assertIn("function storageRemove(", javascript)
        self.assertIn("function storageError(", javascript)
        self.assertIn("control.disabled = true", javascript)
        self.assertIn("자동 저장을 사용할 수 없습니다", javascript)
        self.assertNotIn("localStorage.setItem(", javascript)
        self.assertNotIn("sessionStorage.setItem(", javascript)

    def test_writer_length_limits_are_exposed_to_the_browser(self) -> None:
        javascript = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        markup = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        for limit in ('maxlength="300"', 'maxlength="500"', 'maxlength="1000"', 'maxlength="4000"'):
            self.assertIn(limit, javascript)
        self.assertIn("input.maxLength = 1000", javascript)
        self.assertIn('maxlength="1000" value="${escapeMarkup(state.tools.join(", "))}"', javascript)
        self.assertIn("자동으로 잘라 저장하지 않습니다", markup)

    def test_browser_drafts_clear_on_explicit_auth_boundaries_and_survive_failures(self) -> None:
        javascript = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        clear = javascript.split("function clearBrowserDraft(", 1)[1].split("async function exportAccount()", 1)[0]
        self.assertIn('storageRemove("session", key)', clear)
        self.assertIn('storageRemove("local", REMEMBERED_DRAFT_KEY)', clear)
        self.assertIn("if (previousAccount && previousAccount !== nextAccount) clearBrowserDraft()", javascript)
        logout = javascript.split("async function logout()", 1)[1].split("function clearBrowserDraft(", 1)[0]
        self.assertIn("clearBrowserDraft();", logout)
        expired = javascript.split("if (error.status === 401)", 1)[1].split("} else if (error.status === 409)", 1)[0]
        self.assertNotIn("clearBrowserDraft();", expired)
        self.assertIn("로그인 만료 · 브라우저 초안은 보존됨", expired)
        load_auth_failure = javascript.split("async function loadAuth()", 1)[1].split(
            "function applyAuthState(", 1
        )[0]
        self.assertIn('status: "unknown"', load_auth_failure)
        self.assertNotIn("clearBrowserDraft();", load_auth_failure)
        self.assertIn("브라우저 초안은 그대로 보존했습니다", load_auth_failure)

    def test_account_export_and_deletion_controls_use_csrf_protected_endpoints(self) -> None:
        markup = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        for control in ('id="export-account"', 'id="delete-account"'):
            self.assertIn(control, markup)
        self.assertIn('api("/api/user/export", { method: "POST", body: "{}", csrf: true })', javascript)
        self.assertIn('api("/api/user/delete-account",', javascript)
        self.assertIn("ACCOUNT_DELETE_CONFIRMATION", javascript)
        self.assertIn('storageRemove("session", key)', javascript)


if __name__ == "__main__":
    unittest.main()
