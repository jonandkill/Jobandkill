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
        ):
            self.assertIn(required, markup)

    def test_javascript_never_sends_drafts_outside_same_origin(self) -> None:
        javascript = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        self.assertNotIn("http://", javascript)
        self.assertNotIn("https://", javascript)
        self.assertIn('fetch("/api/drafts/compose"', javascript)
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


if __name__ == "__main__":
    unittest.main()
