"""Check public download selection and project-site paths before deployment."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("site_build", Path(__file__).with_name("build.py"))
site = importlib.util.module_from_spec(spec)
spec.loader.exec_module(site)


def release(version="1.3.1", date="2026-11-07T12:00:00Z", **extra):
    name = f"Anki-Agent-{version}-macOS-arm64-preview.dmg"
    return {"published_at": date, "draft": False,
            "assets": [{"name": name, "browser_download_url":
                f"https://github.com/ajy25/anki-agent/releases/download/v{version}/{name}"}], **extra}


class SiteBuildTests(unittest.TestCase):
    def test_prereleases_can_be_downloaded_but_drafts_cannot(self):
        preview = release(prerelease=True)
        self.assertEqual(site.select_download([preview])["version"], "1.3.1")
        self.assertIsNone(site.select_download([release(draft=True)]))

    def test_latest_published_mac_installer_wins(self):
        older = release("1.3.1", "2026-11-06T12:00:00Z")
        newer = release("1.3.2")
        self.assertEqual(site.select_download([older, newer])["version"], "1.3.2")
        newer["assets"] = []
        self.assertEqual(site.select_download([newer, older])["version"], "1.3.1")

    def test_foreign_or_mismatched_download_urls_are_rejected(self):
        for url in ("javascript:alert(1)", "https://other.example/app.dmg",
                    "https://github.com/other/repo/releases/download/v1/app.dmg",
                    "https://github.com/ajy25/anki-agent/releases/download/v1/wrong.dmg"):
            item = release()
            item["assets"][0]["browser_download_url"] = url
            with self.subTest(url=url):
                self.assertIsNone(site.select_download([item]))

    def test_pending_and_available_pages_build_with_valid_local_links(self):
        for releases, text in (([], "View releases"),
                               ([release()], "Download for Mac")):
            with self.subTest(releases=bool(releases)), tempfile.TemporaryDirectory() as temporary:
                site.build(Path(temporary), releases)
                page = (Path(temporary) / "index.html").read_text()
                self.assertIn(text, page)
                self.assertTrue((Path(temporary) / ".nojekyll").exists())
                self.assertNotIn("__DOWNLOAD_URL__", page)

    def test_new_features_do_not_mislabel_the_existing_download(self):
        for releases, upcoming in (([release()], True), ([release("1.4.0", draft=True), release()], True),
                                    ([release("1.4.0")], False)):
            with self.subTest(upcoming=upcoming), tempfile.TemporaryDirectory() as temporary:
                site.build(Path(temporary), releases)
                page = (Path(temporary) / "index.html").read_text()
                self.assertEqual("Coming in the next release." in page, upcoming)
                self.assertEqual("Available in the current download." in page, not upcoming)
                self.assertIn("Version 1.3.1" if upcoming else "Version 1.4.0", page)
                if upcoming:
                    self.assertNotIn("/releases/download/v1.4.0/", page)
                else:
                    self.assertIn("Add a Groq key, an OpenRouter key, or both", page)

    def test_missing_assets_and_anchors_fail_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            for link in ("missing.css", "#missing", "/style.css"):
                (output / "index.html").write_text(f'<a href="{link}">Link</a>')
                with self.subTest(link=link), self.assertRaises(ValueError):
                    site.check_site(output)


if __name__ == "__main__":
    unittest.main()
