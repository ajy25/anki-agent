"""Build the GitHub Pages site without third-party dependencies."""
from __future__ import annotations

import argparse
import html
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import shutil
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "ajy25/anki-agent"
RELEASES_URL = f"https://github.com/{REPOSITORY}/releases"
ASSET_NAME = re.compile(r"Anki-Agent-(\d+\.\d+\.\d+)-macOS-arm64(?:-preview)?\.dmg")


def select_download(releases: list) -> dict | None:
    if not isinstance(releases, list):
        raise ValueError("Expected a list from GitHub's public releases API.")
    published = [release for release in releases if isinstance(release, dict)
                 and not release.get("draft") and release.get("published_at")]
    for release in sorted(published, key=lambda item: item["published_at"], reverse=True):
        for asset in release.get("assets", []):
            match = ASSET_NAME.fullmatch(asset.get("name", ""))
            url = asset.get("browser_download_url", "")
            parsed = urlsplit(url)
            if (match and parsed.scheme == "https" and parsed.netloc == "github.com"
                    and parsed.path.startswith(f"/{REPOSITORY}/releases/download/")
                    and unquote(parsed.path.rsplit("/", 1)[-1]) == asset["name"]
                    and not parsed.query and not parsed.fragment):
                return {"url": url, "version": match[1]}
    return None


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids, self.links = set(), []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            if attrs["id"] in self.ids:
                raise ValueError(f"Duplicate HTML ID: {attrs['id']}")
            self.ids.add(attrs["id"])
        for attr in ("href", "src"):
            if attrs.get(attr):
                self.links.append(attrs[attr])


def check_site(output: Path) -> None:
    page = (output / "index.html").read_text()
    if re.search(r"__[A-Z_]+__", page):
        raise ValueError("Unresolved template value in the site.")
    links = Links()
    links.feed(page)
    for link in links.links:
        parsed = urlsplit(link)
        if parsed.scheme or parsed.netloc:
            if parsed.scheme != "https":
                raise ValueError(f"Expected a secure external link: {link}")
            continue
        if parsed.path.startswith("/"):
            raise ValueError(f"Use relative paths for the GitHub project site: {link}")
        if parsed.path and parsed.path != "./" and not (output / parsed.path).is_file():
            raise ValueError(f"Missing local website asset: {link}")
        if parsed.fragment and parsed.fragment not in links.ids:
            raise ValueError(f"Missing page anchor: {link}")


def build(output: Path, releases: list) -> None:
    public = ROOT / "site" / "public"
    output = output.resolve()
    # The destination is generated output, never the source or its ancestor.
    if public.resolve().is_relative_to(output) or output.is_relative_to(public.resolve()):
        raise ValueError("Choose a build directory outside site/public.")
    output.mkdir(parents=True, exist_ok=True)
    for path in public.iterdir():
        if path.is_file():
            shutil.copy2(path, output / path.name)
    download = select_download(releases)
    values = {
        "__DOWNLOAD_URL__": download["url"] if download else RELEASES_URL,
        "__DOWNLOAD_LABEL__": "Download for Mac" if download else "View releases",
        "__RELEASE_STATUS__": (f"Version {download['version']} is available. Bring your own deck and Groq key."
                               if download else "The first public Mac download is coming soon. You can explore the source and setup guide today."),
        "__VERSION__": download["version"] if download else (ROOT / "VERSION").read_text().strip(),
    }
    page = (output / "index.html").read_text()
    for key, value in values.items():
        page = page.replace(key, html.escape(value, quote=True))
    (output / "index.html").write_text(page)
    (output / ".nojekyll").touch()
    check_site(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "build" / "site")
    parser.add_argument("--releases", type=Path, help="JSON from the repository's releases API")
    args = parser.parse_args()
    releases = json.loads(args.releases.read_text()) if args.releases else []
    build(args.output, releases)
    print(f"Website built and links checked: {args.output}")


if __name__ == "__main__":
    main()
