"""Resolve Anki media files and rewrite image sources for the web interface."""

from __future__ import annotations

import html
import os
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit


IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".avif": "image/avif",
    ".bmp": "image/bmp",
    ".ico": "image/x-icon",
}


def resolve_media_dir(deck_path: Path) -> Path:
    """Use an explicit MEDIA_DIR, or the collection.media beside the source."""
    raw = os.environ.get("MEDIA_DIR", "").strip()
    if raw:
        return Path(os.path.expandvars(os.path.expanduser(raw))).resolve()
    return (deck_path.parent / "collection.media").resolve()


def is_media_filename(filename: str) -> bool:
    # Anki stores media in a flat directory. Reject paths on every platform.
    return bool(filename) and filename not in (".", "..") and not any(
        c in filename for c in ("/", "\\", "\x00")
    )


def _image_src(src: str) -> str | None:
    src = src.strip()
    if not src:
        return None
    try:
        url = urlsplit(src)
    except ValueError:
        return None
    if url.scheme in ("http", "https") and url.netloc:
        return src
    if not url.scheme and url.netloc:
        return "https:" + src
    if url.scheme:
        return None
    # Anki fields can contain raw filenames (including # and &) as well as
    # URL-encoded filenames. Keep those characters as part of the filename.
    filename = unquote(src)
    if not is_media_filename(filename):
        return None
    return "/media?filename=" + quote(filename, safe="")


class _ImageSources(HTMLParser):
    """Rewrite only img tags in already sanitized HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.chunks: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag != "img":
            self.chunks.append(self.get_starttag_text())
            return
        image_attrs = dict(attrs)
        src = _image_src(image_attrs.pop("src", "") or "")
        if src:
            image_attrs["src"] = src
        image_attrs["loading"] = "lazy"
        image_attrs["decoding"] = "async"
        attributes = "".join(
            f' {key}="{html.escape(value or "", quote=True)}"'
            for key, value in image_attrs.items()
        )
        self.chunks.append(f"<img{attributes}>")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        self.chunks.append(f"</{tag}>")

    def handle_data(self, data):
        self.chunks.append(data)

    def handle_entityref(self, name):
        self.chunks.append(f"&{name};")

    def handle_charref(self, name):
        self.chunks.append(f"&#{name};")


def rewrite_image_sources(sanitized_html: str) -> str:
    parser = _ImageSources()
    parser.feed(sanitized_html)
    parser.close()
    return "".join(parser.chunks)
