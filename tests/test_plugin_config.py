from __future__ import annotations

import json
from pathlib import Path
import re
import unittest


class PonytailPluginConfigurationTests(unittest.TestCase):
    def test_remote_plugin_is_pinned_to_the_reviewed_commit(self) -> None:
        path = Path(__file__).resolve().parents[1] / ".agents" / "plugins" / "marketplace.json"
        marketplace = json.loads(path.read_text(encoding="utf-8"))
        plugin = next(item for item in marketplace["plugins"] if item["name"] == "ponytail")
        self.assertEqual(plugin["source"]["source"], "url")
        self.assertEqual(
            plugin["source"]["url"],
            "https://github.com/DietrichGebert/ponytail.git",
        )
        self.assertEqual(
            plugin["source"]["ref"],
            "0a4dd63ad4541f4f655c4108a295916f3c1d8fda",
        )
        self.assertRegex(plugin["source"]["ref"], re.compile(r"^[0-9a-f]{40}$"))


if __name__ == "__main__":
    unittest.main()
