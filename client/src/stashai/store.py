"""All bookmarks held locally, fast searches over them, and named result sets (S1, S2, …).

The model never copies long lists of ids: a search or a check creates a set, and the model refers
to it by name (“delete S4”). The program expands names into ids, so nothing gets lost or invented.
"""
from __future__ import annotations

import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlsplit


@dataclass
class Bookmark:
    id: int
    url: str
    title: str
    notes: str
    host: str
    tags: list[str]
    tab: str
    category: str
    category_id: int | None
    created_at: int
    position: int = 0
    text: str = ""

    @property
    def where(self) -> str:
        return f"{self.tab} / {self.category}" if self.category_id else "Catalog"


@dataclass
class NamedSet:
    name: str
    ids: list[int]
    label: str
    origin: str = ""
    created: float = field(default_factory=time.time)


def bare_host(host: str) -> str:
    return host.lower().removeprefix("www.")


def short_url(url: str, n: int = 60) -> str:
    try:
        p = urlsplit(url)
    except ValueError:
        return url[:n]
    rest = (p.path or "") + (f"?{p.query}" if p.query else "")
    s = bare_host(p.hostname or "") + (rest if rest != "/" else "") if p.hostname else url
    return s if len(s) <= n else s[: n - 1] + "…"


def _date(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(str(value)).timestamp())
    except ValueError:
        raise ValueError(f"not a date (YYYY-MM-DD): {value!r}") from None


class Store:
    def __init__(self, snapshot: dict | None = None):
        self.bookmarks: dict[int, Bookmark] = {}
        self.tabs: list[dict] = []
        self.tags: list[tuple[str, int]] = []
        self.usage: dict[int, dict] = {}         # bookmark id -> visit counts from the browsing history
        self.history_sources: list[dict] = []
        self.sets: dict[str, NamedSet] = {}
        self._next = 1
        self.loaded_at = 0.0
        if snapshot:
            self.load(snapshot)

    def load(self, snap: dict) -> None:
        self.bookmarks = {}
        for b in snap["bookmarks"]:
            bm = Bookmark(id=b["id"], url=b["url"], title=b["title"], notes=b.get("notes") or "",
                          host=b.get("host") or "", tags=list(b.get("tags") or []), tab=b.get("tab") or "",
                          category=b.get("category") or "", category_id=b.get("category_id"),
                          created_at=b.get("created_at") or 0, position=b.get("position") or 0)
            bm.text = "\n".join((bm.title, bm.url, bm.notes, " ".join(bm.tags), bm.tab, bm.category)).casefold()
            self.bookmarks[bm.id] = bm
        self.tabs = snap.get("tabs", [])
        self.tags = [(t, n) for t, n in snap.get("tags", [])]
        self.loaded_at = time.time()

    def load_usage(self, data: dict) -> None:
        self.usage = {int(k): v for k, v in (data.get("usage") or {}).items()}
        self.history_sources = data.get("sources") or []

    def use(self, i: int) -> tuple[int, int, int]:
        """Sort key for "most used": visits in 90 days, in a year, in all."""
        u = self.usage.get(i) or {}
        return u.get("visits_90d", 0), u.get("visits_365d", 0), u.get("visits", 0)

    def use_text(self, i: int) -> str:
        u = self.usage.get(i)
        if not u:
            return "visits: none" if self.history_sources else ""
        last = time.strftime("%Y-%m-%d", time.localtime(u["last_visit"])) if u.get("last_visit") else "-"
        return f"visits 30d {u['visits_30d']}, 90d {u['visits_90d']}, all {u['visits']}, last {last}"

    # --- sets ------------------------------------------------------------------

    def new_set(self, ids, label: str, origin: str = "") -> NamedSet:
        name = f"S{self._next}"
        self._next += 1
        s = NamedSet(name, list(dict.fromkeys(ids)), label[:200], origin[:300])
        self.sets[name] = s
        return s

    def resolve(self, name: str) -> list[int]:
        key = str(name).strip().upper()
        if key == "ALL":
            return list(self.bookmarks)
        if key not in self.sets:
            known = ", ".join(self.sets) or "none yet"
            raise KeyError(f"no set named {name!r} (sets: {known}; ALL = every bookmark)")
        return [i for i in self.sets[key].ids if i in self.bookmarks]

    # --- search ----------------------------------------------------------------

    def search(self, *, text=None, match: str = "any", regex: str = "", host=None, tags_any=None, tags_all=None,
               tags_none=None, untagged: bool = False, tab: str = "", category: str = "", where: str = "",
               in_set: str = "", not_in_set: str = "", ids=None, exclude_ids=None, used_min: int = 0,
               unused_days: int = 0, sort: str = "", added_after: str = "", added_before: str = "",
               **unknown) -> list[int]:
        if unknown:
            raise ValueError(f"unknown search fields: {', '.join(unknown)} (use text, match, regex, host, tags_any, "
                             "tags_all, tags_none, untagged, tab, category, where, in_set, not_in_set, ids, exclude_ids, "
                             "used_min, unused_days, sort, added_after, added_before)")
        as_list = lambda v: [v] if isinstance(v, str) else list(v or [])  # noqa: E731
        terms = [t.casefold().strip() for t in as_list(text) if str(t).strip()]
        patterns = []
        for t in terms:
            # short terms ("x", "ai") match whole words only, or they would match half the collection
            patterns.append(re.compile(rf"(?<![\w]){re.escape(t)}(?![\w])") if len(t) <= 3 else None)
        rx = re.compile(regex, re.I) if regex else None
        hosts = [bare_host(h) for h in as_list(host) if h]
        t_any = {t.lower() for t in as_list(tags_any)}
        t_all = {t.lower() for t in as_list(tags_all)}
        t_none = {t.lower() for t in as_list(tags_none)}
        after, before = _date(added_after), _date(added_before)
        pool = self.resolve(in_set) if in_set else list(self.bookmarks)
        excluded = set(self.resolve(not_in_set)) if not_in_set else set()
        excluded |= {int(i) for i in as_list(exclude_ids)}
        wanted = {int(i) for i in as_list(ids)} if ids else None
        out = []
        for i in pool:
            b = self.bookmarks[i]
            if i in excluded or (wanted is not None and i not in wanted):
                continue
            if terms:
                hits = [(p.search(b.text) is not None) if p else (t in b.text) for t, p in zip(terms, patterns)]
                if not (all(hits) if match == "all" else any(hits)):
                    continue
            if rx and not rx.search(b.text):
                continue
            if hosts and not any(bare_host(b.host) == h or b.host.endswith("." + h) for h in hosts):
                continue
            tags = set(b.tags)
            if t_any and not tags & t_any or t_all and not t_all <= tags or tags & t_none:
                continue
            if untagged and tags:
                continue
            if tab and b.tab.casefold() != tab.casefold():
                continue
            if category and b.category.casefold() != category.casefold():
                continue
            if where == "catalog" and b.category_id or where == "dashboard" and not b.category_id:
                continue
            if after and b.created_at < after or before and b.created_at >= before:
                continue
            if used_min and self.use(i)[0] < int(used_min):
                continue
            if unused_days:
                last = (self.usage.get(i) or {}).get("last_visit") or 0
                if last > time.time() - int(unused_days) * 86400:
                    continue
            out.append(i)
        if sort == "use":
            out.sort(key=self.use, reverse=True)
        elif sort == "position":
            out.sort(key=lambda i: (self.bookmarks[i].where, self.bookmarks[i].position))
        return out

    # --- text for the model and the screen ------------------------------------------

    def line(self, i: int, *, notes: bool = True) -> str:
        b = self.bookmarks[i]
        parts = [f"#{b.id} {b.title[:100]}", short_url(b.url), "tags: " + (", ".join(b.tags) or "-"), b.where]
        if notes and b.notes:
            parts.append("notes: " + " ".join(b.notes.split())[:100])
        if self.history_sources:
            parts.append(self.use_text(i))
        return " | ".join(parts)

    def describe(self, ids: list[int], *, sample: int = 15, whole: int = 60, name: str = "") -> str:
        """Size, tags, sites and places of a set, then its lines: all of them when there are at most
        *whole*, so the model has every id at hand without asking for them."""
        if not ids:
            return "0 bookmarks"
        tags = Counter(t for i in ids for t in self.bookmarks[i].tags)
        hosts = Counter(bare_host(self.bookmarks[i].host) or "-" for i in ids)
        places = Counter(self.bookmarks[i].where for i in ids)
        untagged = sum(1 for i in ids if not self.bookmarks[i].tags)
        out = [f"{len(ids)} bookmarks",
               "tags: " + ", ".join(f"{t} {n}" for t, n in tags.most_common(12)) + (f"; untagged {untagged}" if untagged else ""),
               "sites: " + ", ".join(f"{h} {n}" for h, n in hosts.most_common(8)),
               "places: " + ", ".join(f"{p} {n}" for p, n in places.most_common(6))]
        if self.history_sources:
            used = sum(1 for i in ids if self.use(i)[0])
            out.append(f"used in the last 90 days: {used} of {len(ids)}")
        shown = len(ids) if len(ids) <= whole else sample
        out += [self.line(i) for i in ids[:shown]]
        if len(ids) > shown:
            ref = name or "the set"
            out.append(f"… and {len(ids) - shown} more: show {{\"set\": \"{ref}\", \"offset\": {shown}}} lists them "
                       "with their ids (only needed if you must act on them one by one)")
        return "\n".join(out)

    def overview(self, *, max_tags: int = 400) -> str:
        catalog = sum(1 for b in self.bookmarks.values() if not b.category_id)
        lines = [f"{len(self.bookmarks)} bookmarks ({catalog} in the Catalog, the rest on the Dashboard)",
                 f"TAGS ({len(self.tags)}, with counts): " + ", ".join(f"{t} {n}" for t, n in self.tags[:max_tags])]
        if len(self.tags) > max_tags:
            lines[-1] += f", … {len(self.tags) - max_tags} more"
        if self.history_sources:
            srcs = ", ".join(f"{s['source']} ({time.strftime('%Y-%m-%d', time.localtime(s['synced_at'])) if s.get('synced_at') else '?'})"
                             for s in self.history_sources)
            used = sum(1 for i in self.bookmarks if self.use(i)[0])
            lines.append(f"BROWSING HISTORY from {srcs}: {used} bookmarks visited in the last 90 days")
        lines.append("DASHBOARD (tab: categories with counts):")
        for t in self.tabs:
            cats = ", ".join(f"{c['name']} {c['count']}" for c in t["categories"])
            lines.append(f"- {t['name']}: {cats or '(empty)'}")
        return "\n".join(lines)

    def sets_text(self) -> str:
        if not self.sets:
            return "(none yet)"
        return "\n".join(f"{s.name}: {len(self.resolve(s.name))} bookmarks – {s.label}" for s in self.sets.values())
