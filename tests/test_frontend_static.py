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

    def test_javascript_never_sends_drafts_outside_same_origin(self) -> None:
        javascript = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        self.assertNotIn("http://", javascript)
        self.assertNotIn("https://", javascript)
        self.assertIn('fetch("/api/drafts/compose"', javascript)


if __name__ == "__main__":
    unittest.main()
