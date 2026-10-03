"""Netscape bookmark file (the HTML format every browser exports) parsing and writing."""
from __future__ import annotations

import html
import time
from html.parser import HTMLParser


class NetscapeParser(HTMLParser):
    """Collects {url, title, notes, tags, add_date, path, catalog} for every <A> in the file."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[dict] = []
        self._stack: list[tuple[str, bool] | None] = []
        self._pending: tuple[str, bool] | None = None
        self._mode: str | None = None
        self._buf = ""
        self._attrs: dict = {}
        self._last: dict | None = None

    def _flush_dd(self) -> None:
        if self._mode == "dd":
            if self._last is not None and self._buf.strip():
                self._last["notes"] = self._buf.strip()
            self._mode = None

    def handle_starttag(self, tag, attrs):
        if tag in ("dt", "dl", "h3", "a", "dd"):
            self._flush_dd()
        if tag in ("h3", "a"):
            self._mode, self._buf, self._attrs = tag, "", {k.lower(): v or "" for k, v in attrs}
        elif tag == "dl":
            self._stack.append(self._pending)
            self._pending = None
        elif tag == "dd":
            self._mode, self._buf = "dd", ""

    def handle_endtag(self, tag):
        if tag == "h3" and self._mode == "h3":
            self._pending = (self._buf.strip() or "?", "stash_catalog" in self._attrs)
            self._mode = None
        elif tag == "a" and self._mode == "a":
            folders = [f for f in self._stack if f]
            tags = [t for t in self._attrs.get("tags", "").split(",") if t.strip()]
            self._last = {
                "url": self._attrs.get("href", "").strip(),
                "title": self._buf.strip(),
                "notes": self._attrs.get("notes", "").strip(),  # NOTES attribute of some bookmark managers; <DD> text overrides
                "tags": tags,
                "add_date": _timestamp(self._attrs.get("add_date", "")),
                "path": [name for name, _ in folders],
                "catalog": any(flag for _, flag in folders),
            }
            self.items.append(self._last)
            self._mode = None
        elif tag == "dl":
            self._flush_dd()
            if self._stack:
                self._stack.pop()
            self._last = None

    def handle_data(self, data):
        if self._mode:
            self._buf += data


def _timestamp(raw: str) -> int:
    try:
        value = int(float(raw))
    except ValueError:
        return 0
    while value > 10 ** 11:  # milliseconds / microseconds since epoch
        value //= 1000
    return value if 0 < value <= int(time.time()) + 86400 else 0


def parse(text: str) -> list[dict]:
    parser = NetscapeParser()
    parser.feed(text)
    parser.close()
    return parser.items


def export(tabs: list[dict], catalog: list[dict], catalog_name: str = "Catalog") -> str:
    """tabs: [{name, categories: [{name, bookmarks: [...]}]}]; bookmarks: {url,title,notes,tags,created_at}."""
    e = html.escape
    out = [
        "<!DOCTYPE NETSCAPE-Bookmark-file-1>",
        "<!-- This is an automatically generated file. -->",
        '<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">',
        "<TITLE>Bookmarks</TITLE>",
        "<H1>Bookmarks</H1>",
        "<DL><p>",
    ]

    def links(bookmarks, indent):
        for b in bookmarks:
            tags = f' TAGS="{e(",".join(b["tags"]))}"' if b["tags"] else ""
            out.append(f'{indent}<DT><A HREF="{e(b["url"])}" ADD_DATE="{b["created_at"]}"{tags}>{e(b["title"])}</A>')
            if b["notes"]:
                out.append(f"{indent}<DD>{e(b['notes'])}")

    for tab in tabs:
        out.append(f"    <DT><H3>{e(tab['name'])}</H3>")
        out.append("    <DL><p>")
        for cat in tab["categories"]:
            out.append(f"        <DT><H3>{e(cat['name'])}</H3>")
            out.append("        <DL><p>")
            links(cat["bookmarks"], " " * 12)
            out.append("        </DL><p>")
        out.append("    </DL><p>")
    if catalog:
        out.append(f"    <DT><H3 STASH_CATALOG=\"1\">{e(catalog_name)}</H3>")
        out.append("    <DL><p>")
        links(catalog, " " * 8)
        out.append("    </DL><p>")
    out.append("</DL><p>")
    return "\n".join(out) + "\n"
