import importlib.util
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("merqato_server", ROOT / "app" / "server.py")
server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server)


class UrlValidationTests(unittest.TestCase):
    def test_accepts_plain_http_urls(self):
        for value in ("https://example.com", "http://example.com/path?x=1"):
            cleaned, error = server.clean_url(value)
            self.assertIsNone(error)
            self.assertEqual(cleaned, value)

    def test_rejects_invalid_or_unsafe_urls(self):
        for value in (
            "not-a-url",
            "javascript:alert(1)",
            "https://user:password@example.com",
            "ftp://example.com",
            "https://[bad",
            "https://example.com/\njavascript:alert(1)",
            "https://example.com:bad",
            "https://example.com:99999",
            "https://:80",
        ):
            cleaned, error = server.clean_url(value)
            self.assertIsNone(cleaned)
            self.assertTrue(error)


class StarterLinkTests(unittest.TestCase):
    def test_golden_goose_uses_the_current_repository(self):
        links = server.seed_links()
        golden_goose = [
            link for link in links
            if link["name"] == "Golden Goose Eggs Work"
        ]

        self.assertEqual(len(golden_goose), 1)
        self.assertEqual(
            golden_goose[0]["url"],
            "https://github.com/palawan-collective/golden-goose-eggs-work",
        )
        self.assertFalse(any(
            link["url"] == "https://sanvic.merqato.digital"
            for link in links
        ))


class IconValidationTests(unittest.TestCase):
    def test_accepts_lucide_style_names(self):
        for value in ("folder-git-2", "cloud_cog", "triangle"):
            cleaned, error = server.clean_icon(value)
            self.assertIsNone(error)
            self.assertEqual(cleaned, value)

    def test_rejects_non_text_or_dangerous_icon_values(self):
        for value in ({"name": "triangle"}, ["triangle"], "has space", "__proto__"):
            cleaned, error = server.clean_icon(value)
            self.assertIsNone(cleaned)
            self.assertTrue(error)

    def test_import_validation_normalizes_missing_icons(self):
        fields, error = server.validate({
            "name": "Example",
            "url": "https://example.com",
            "category": "Other",
        })
        self.assertIsNone(error)
        self.assertEqual(fields["icon"], "link")


if __name__ == "__main__":
    unittest.main()
