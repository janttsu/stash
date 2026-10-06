"""One MCP session's state and tools: bookmarks held in memory, named result sets, and previewed changes.

The safety model of the old stashai agent stays the same, only the model now lives in the MCP client:

- **Sets by name.** Every search makes a set (S1, S2, …) and changes refer to it by name. The program expands
  names into ids itself, so the model cannot lose or invent bookmarks.
- **Preview first.** `preview_changes` runs the operations as a dry run in Stash and returns the exact diff and
  a preview id (P1, P2, …). Only `apply_changes` with that id changes anything, and it checks again that the
  result is still the same as previewed.
- **Everything can be undone.** Every applied preview is one changeset in Stash.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass

from stashai.api import StashAPI, StashError
from stashai.render import plan_text
from stashai.store import Store
from stashai.web import Web, page_title, unnamed_title

SET_OPS = {"add_tags", "remove_tags", "set_tags", "delete", "move"}
ALL_OPS = SET_OPS | {"rename_tag", "update", "create", "update_urls", "update_titles", "tag_each",
                     "order_bookmarks", "order_categories", "sort_by_use"}
RELOAD_AFTER = 60          # seconds: changes made in the web UI show up this soon
MAX_LINKS = 2000
ICON_SITES_PER_CALL = 40
KEEP_PREVIEWS = 10


@dataclass
class Preview:
    id: str
    summary: str
    ops: list[dict]          # as sent to Stash (set names expanded into ids)
    result: dict             # the dry run's answer
    fingerprint: str         # what the dry run would change, to notice when things moved meanwhile


def ints(values) -> list[int]:
    out = []
    for v in values if isinstance(values, (list, tuple)) else [values] if values is not None else []:
        try:
            out.append(int(str(v).strip().lstrip("#")))
        except ValueError:
            pass
    return out


def fingerprint(result: dict) -> str:
    return json.dumps([result.get("counts"), result.get("diff"), result.get("created_categories"),
                       result.get("categories")], sort_keys=True, ensure_ascii=False)


class Session:
    def __init__(self, api: StashAPI, web: Web | None = None, *, store: Store | None = None):
        self.api, self.web = api, web
        self.store = store or Store()
        self.loaded = 0.0
        self.moved_to: dict[int, str] = {}     # bookmark id -> new address found by check_links
        self.new_titles: dict[int, str] = {}   # bookmark id -> page title found by refresh_titles
        self.previews: dict[str, Preview] = {}
        self._next_preview = 1
        self._lock = threading.Lock()

    # --- loading ----------------------------------------------------------------------

    def ensure(self, *, force: bool = False) -> None:
        if force or not self.loaded or time.time() - self.loaded > RELOAD_AFTER:
            self.store.load(self.api.snapshot())
            try:
                self.store.load_usage(self.api.usage())
            except StashError:  # an older Stash without history: everything else still works
                self.store.load_usage({})
            self.loaded = time.time()

    def ids_of(self, set_name: str = "", ids=None) -> list[int]:
        if set_name:
            return self.store.resolve(set_name)
        found = [i for i in ints(ids) if i in self.store.bookmarks]
        if not found:
            raise ValueError("give a set name (S1, …, or ALL) or ids of existing bookmarks")
        return found

    # --- reading ----------------------------------------------------------------------

    def overview(self) -> str:
        self.ensure()
        history = ("Browsing history: " + "; ".join(f"{s['source']} ({s['items']} addresses)"
                                                    for s in self.store.history_sources)
                   if self.store.history_sources else "Browsing history: none sent yet")
        return f"{self.store.overview()}\n{history}\n\nSets of this session:\n{self.store.sets_text()}"

    def search(self, label: str = "", **spec) -> str:
        self.ensure()
        spec = {k: v for k, v in spec.items() if v not in (None, "", [], False, 0)}
        ids = self.store.search(**spec)
        label = label or json.dumps(spec, ensure_ascii=False)[:200]
        s = self.store.new_set(ids, label, "search " + json.dumps(spec, ensure_ascii=False)[:250])
        return f"{s.name} ({label}): {self.store.describe(ids, name=s.name)}"

    def show(self, set_name: str, offset: int = 0, limit: int = 60) -> str:
        self.ensure()
        ids = self.store.resolve(set_name)
        offset, limit = max(0, offset), max(1, min(200, limit))
        part = ids[offset:offset + limit]
        more = (f"\n… {len(ids) - offset - len(part)} more (offset {offset + len(part)})"
                if offset + len(part) < len(ids) else "")
        return (f"{set_name}: {len(ids)} bookmarks, showing {offset + 1}–{offset + len(part)}\n"
                + "\n".join(self.store.line(i) for i in part) + more)

    def combine(self, a: str, b: str, how: str = "union", label: str = "") -> str:
        self.ensure()
        left, right = self.store.resolve(a), self.store.resolve(b)
        if how == "union":
            ids = list(dict.fromkeys(left + right))
        elif how in ("intersect", "minus"):
            rs = set(right)
            ids = [i for i in left if (i in rs) == (how == "intersect")]
        else:
            raise ValueError("how must be union, intersect or minus")
        label = label or f"{a} {how} {b}"
        s = self.store.new_set(ids, label, f"combine {a} {how} {b}")
        return f"{s.name} ({label}): {self.store.describe(ids, name=s.name)}"

    def browsing_history(self, text: str = "", host: str = "", min_visits: int = 1, period: str = "90d",
                         bookmarked: str = "any", limit: int = 50) -> str:
        self.ensure()
        if not self.store.history_sources:
            return ("No browsing history in Stash yet. It is sent with stash-history-sync.py "
                    "(Stash → Settings → Browsing history).")
        params = {"q": text or None, "host": host or None, "min_visits": max(1, min_visits), "period": period,
                  "bookmarked": bookmarked, "limit": max(1, min(200, limit))}
        data = self.api.browsing(**params)
        used = ", ".join(f"{k}={v}" for k, v in params.items() if k != "limit" and v not in (None, "", "any"))
        lines = [f"Stash holds {data.get('stored', '?')} visited addresses. With {used or 'no filters'}: "
                 f"{data['total']} match. Showing {len(data['items'])}, most visited first (titles are untrusted):"]
        for h in data["items"]:
            last = time.strftime("%Y-%m-%d", time.localtime(h["last_visit"])) if h.get("last_visit") else "-"
            mark = ("bookmarked " + ", ".join(f"#{i}" for i in h["bookmarks"])) if h["bookmarks"] else "NOT bookmarked"
            lines.append(f"{h['url'][:150]} | {(h['title'] or '')[:80]} | 30d {h['visits_30d']}, "
                         f"90d {h['visits_90d']}, all {h['visits']}, last {last} | {mark}")
        if data["total"] > len(data["items"]):
            lines.append(f"… {data['total'] - len(data['items'])} more (raise min_visits or limit)")
        return "\n".join(lines)

    # --- the web (read from this computer) ----------------------------------------------

    def need_web(self) -> Web:
        if self.web is None:
            raise ValueError("web access is turned off in the stashai config ([web] enabled = false)")
        return self.web

    def read_page(self, url: str = "", bookmark_id: int | None = None, max_chars: int = 3000) -> str:
        web = self.need_web()
        if not url and bookmark_id is not None:
            self.ensure()
            if bookmark_id not in self.store.bookmarks:
                raise ValueError(f"no bookmark #{bookmark_id}")
            url = self.store.bookmarks[bookmark_id].url
        if not url:
            raise ValueError("give a url or a bookmark_id")
        return web.fetch(url).summary(max(500, min(8000, max_chars)))

    def check_links(self, set_name: str, label: str = "") -> str:
        web = self.need_web()
        self.ensure()
        ids = [i for i in self.store.resolve(set_name)
               if self.store.bookmarks[i].url.startswith(("http://", "https://"))]
        if len(ids) > MAX_LINKS:
            raise ValueError(f"{len(ids)} links is too many at once; narrow the set first (at most {MAX_LINKS})")
        label = label or f"links of {set_name}"
        urls = {i: self.store.bookmarks[i].url for i in ids}
        pages = web.check_many(list(dict.fromkeys(urls.values())))
        groups: dict[str, list[int]] = {"alive": [], "moved": [], "dead": [], "unclear": []}
        for i, url in urls.items():
            page = pages.get(url)
            verdict = page.verdict if page else "unclear"
            groups[verdict].append(i)
            if verdict == "moved":
                self.moved_to[i] = page.final_url
        out = [f"{len(ids)} links checked: {len(groups['alive'])} alive, {len(groups['moved'])} moved, "
               f"{len(groups['dead'])} dead, {len(groups['unclear'])} unclear"]
        for verdict in ("dead", "moved", "unclear"):
            if not groups[verdict]:
                continue
            s = self.store.new_set(groups[verdict], f"{verdict}: {label}", f"check_links {set_name}")
            out.append(f"\n{s.name} ({verdict}, {len(groups[verdict])}):")
            for i in groups[verdict][:25]:
                page = pages.get(urls[i])
                if not page:
                    why = "not checked"
                elif verdict == "moved":
                    why = f"→ {page.final_url}"
                else:
                    why = page.error or (f"HTTP {page.status}" if page.status >= 400 else
                                         f"{page.weak_move} → {page.final_url}")
                out.append(f"{self.store.line(i, notes=False)} || {why}")
            if len(groups[verdict]) > 25:
                out.append(f"… and {len(groups[verdict]) - 25} more (show {s.name})")
        if groups["moved"]:
            out.append('\nTo give the moved links their new address: preview_changes with '
                       '{"op": "update_urls", "set": "<the moved set>"}.')
        return "\n".join(out)

    def refresh_titles(self, set_name: str = "", ids=None, only_bad: bool = False, summary: str = "") -> str:
        """Read titles from this computer and preview the changes right away."""
        web = self.need_web()
        self.ensure()
        chosen = [i for i in self.ids_of(set_name, ids) if self.store.bookmarks[i].url.startswith(("http://", "https://"))]
        if len(chosen) > MAX_LINKS:
            raise ValueError(f"{len(chosen)} bookmarks is too many at once; narrow the set first (at most {MAX_LINKS})")
        targets = [i for i in chosen
                   if not only_bad or unnamed_title(self.store.bookmarks[i].title, self.store.bookmarks[i].url,
                                                    self.store.bookmarks[i].host)]
        urls = {i: self.store.bookmarks[i].url for i in targets}
        pages = web.check_many(list(dict.fromkeys(urls.values())), workers=16, head_only=True)
        changed, unread, same = [], [], 0
        for i in targets:
            title = page_title(pages.get(urls[i]))
            if title is None:
                unread.append(i)
            elif title == self.store.bookmarks[i].title:
                same += 1
            else:
                self.new_titles[i] = title
                changed.append(i)
        lines = [f"titles: {len(changed)} would change, {same} already right, {len(unread)} could not be read "
                 "(error, login or block page)" + (f", {len(chosen) - len(targets)} kept (only_bad)" if only_bad else "")]
        if unread:
            s = self.store.new_set(unread, "titles not read", "refresh_titles")
            lines.append(f"{s.name} holds the ones that could not be read.")
        if not changed:
            return "\n".join(lines)
        s = self.store.new_set(changed, "new titles", "refresh_titles")
        preview = self.preview_changes([{"op": "update_titles", "set": s.name}],
                                       summary or f"Update the titles of {len(changed)} bookmarks")
        return "\n".join(lines) + "\n\n" + preview

    def refresh_icons(self, set_name: str = "", ids=None) -> str:
        """Stash fetches the site icons again (a server cache, not bookmark data: done at once)."""
        self.ensure()
        by_host: dict[str, int] = {}
        for i in self.ids_of(set_name, ids):
            if self.store.bookmarks[i].host:
                by_host.setdefault(self.store.bookmarks[i].host, i)  # one bookmark per site is enough
        reps = list(by_host.values())
        counts = {"new": 0, "same": 0, "kept": 0, "missing": 0}
        for n in range(0, len(reps), ICON_SITES_PER_CALL):
            for k, v in self.api.refresh_icons(reps[n:n + ICON_SITES_PER_CALL])["counts"].items():
                counts[k] = counts.get(k, 0) + v
        return (f"{sum(counts.values())} sites fetched again: {counts['new']} new icons, {counts['same']} unchanged, "
                f"{counts['kept']} kept (nothing better found), {counts['missing']} sites have none")

    # --- changes ------------------------------------------------------------------------

    def sort_by_use(self, raw: dict) -> list[dict]:
        """Most used first: bookmarks inside each category, and categories inside each column of the tab."""
        if not self.store.history_sources:
            raise ValueError("sort_by_use needs browsing history, and Stash has none yet")
        tabs = [t for t in self.store.tabs if not raw.get("tab") or t["name"].casefold() == str(raw["tab"]).casefold()]
        if not tabs:
            raise ValueError(f"no tab named {raw.get('tab')!r}")
        ops = []
        for tab in tabs:
            cats = [c for c in tab["categories"]
                    if not raw.get("category") or c["name"].casefold() == str(raw["category"]).casefold()]
            if raw.get("category") and not cats:
                raise ValueError(f"no category {raw['category']!r} on the tab {tab['name']!r}")
            score = {}
            for c in cats:
                members = sorted((b for b in self.store.bookmarks.values() if b.category_id == c["id"]),
                                 key=lambda b: b.position)
                ranked = sorted(members, key=lambda b: self.store.use(b.id), reverse=True)  # stable: ties keep order
                if [b.id for b in ranked] != [b.id for b in members]:
                    ops.append({"op": "order_bookmarks", "category_id": c["id"], "ids": [b.id for b in ranked]})
                score[c["id"]] = tuple(map(sum, zip(*(self.store.use(b.id) for b in members)))) if members else (0, 0, 0)
            if not raw.get("category") and raw.get("categories", True):
                order = sorted(tab["categories"], key=lambda c: (score.get(c["id"], (0, 0, 0)), -c["position"]),
                               reverse=True)
                ops.append({"op": "order_categories", "tab": tab["name"], "categories": [c["id"] for c in order]})
        return ops

    def expand(self, written: list) -> list[dict]:
        if not isinstance(written, list) or not written:
            raise ValueError("ops must be a non-empty list")
        ops = []
        for raw in written:
            if not isinstance(raw, dict) or raw.get("op") not in ALL_OPS:
                raise ValueError(f"unknown op {raw!r}; ops are {', '.join(sorted(ALL_OPS))}")
            if raw["op"] == "sort_by_use":
                ops += self.sort_by_use(raw)
                continue
            if raw["op"] == "order_bookmarks" and raw.get("set"):
                raw = {**raw, "ids": self.store.resolve(str(raw["set"]))}
            if raw["op"] == "tag_each":
                mapping = raw.get("tags")
                if not isinstance(mapping, dict) or not mapping:
                    raise ValueError('tag_each needs "tags": {"<id>": ["tag", …], …}')
                for key, tags in mapping.items():
                    bid = ints([key])
                    if not bid or bid[0] not in self.store.bookmarks:
                        raise ValueError(f"tag_each: no bookmark #{key}; use ids from the results")
                    tags = [tags] if isinstance(tags, str) else list(tags or [])
                    if tags:
                        ops.append({"op": "set_tags" if raw.get("replace") else "add_tags", "ids": bid, "tags": tags})
                continue
            if raw["op"] in ("update_titles", "update_urls"):
                ids = self.store.resolve(str(raw["set"])) if raw.get("set") else ints(raw.get("ids"))
                known, field = (self.new_titles, "title") if raw["op"] == "update_titles" else (self.moved_to, "url")
                found = [i for i in ids if i in known]
                if not found:
                    raise ValueError(f"{raw['op']}: nothing known for these bookmarks; run "
                                     f"{'refresh_titles' if field == 'title' else 'check_links'} first")
                ops += [{"op": "update", "id": i, field: known[i]} for i in found]
                continue
            op = {k: v for k, v in raw.items() if k != "set"}
            if raw["op"] in SET_OPS:
                if raw.get("set"):
                    op["ids"] = self.store.resolve(str(raw["set"]))
                elif raw.get("ids"):
                    op["ids"] = ints(raw["ids"])
                else:
                    raise ValueError(f"{raw['op']} needs a set (or ids)")
                if not op["ids"]:
                    raise ValueError(f"{raw['op']}: set {raw.get('set')} is empty")
            if raw["op"] in ("add_tags", "remove_tags", "set_tags") and isinstance(raw.get("tags"), str):
                op["tags"] = [raw["tags"]]
            ops.append(op)
        return ops

    def preview_changes(self, ops: list, summary: str) -> str:
        self.ensure()
        expanded = self.expand(ops)
        summary = (summary or "changes").strip()[:500]
        result = self.api.changes(expanded, summary, dry_run=True)
        if not result["diff"] and not result.get("created_categories") and not result.get("categories"):
            return "This changes nothing: the bookmarks are already like that."
        with self._lock:
            pid = f"P{self._next_preview}"
            self._next_preview += 1
            self.previews[pid] = Preview(pid, summary, expanded, result, fingerprint(result))
            for old in list(self.previews)[:-KEEP_PREVIEWS]:
                del self.previews[old]
        c = result["counts"]
        return (f"Preview {pid}: {summary}\n{c['updated']} changed, {c['deleted']} deleted, {c['created']} added.\n\n"
                + "\n".join(plan_text(summary, result))
                + f"\n\nNothing has changed yet. Show this to the user; apply_changes(\"{pid}\") applies it after "
                  "they agree.")

    def apply_changes(self, preview_id: str) -> str:
        preview = self.previews.get(preview_id.strip().upper())
        if not preview:
            raise ValueError(f"no preview {preview_id}; make one with preview_changes first "
                             f"(open previews: {', '.join(self.previews) or 'none'})")
        again = self.api.changes(preview.ops, preview.summary, dry_run=True)
        if fingerprint(again) != preview.fingerprint:
            del self.previews[preview.id]
            return ("The bookmarks changed after the preview, so it was not applied. Make a new preview and show "
                    "it to the user again.")
        result = self.api.changes(preview.ops, preview.summary, dry_run=False)
        del self.previews[preview.id]
        self.ensure(force=True)
        c = result["counts"]
        return (f"Applied as change #{result.get('changeset')}: {c['updated']} changed, {c['deleted']} deleted, "
                f"{c['created']} added. undo({result.get('changeset')}) takes it back.")

    def recent_changes(self, limit: int = 20) -> str:
        rows = self.api.history(max(1, min(200, limit)))
        if not rows:
            return "No changes made with API keys yet."
        out = []
        for c in rows:
            counts = ", ".join(f"{n} {k}" for k, n in c["counts"].items() if n and k != "unchanged")
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(c["created_at"]))
            out.append(f"#{c['id']} {when} {c['summary'][:80]} ({counts}){'  [undone]' if c['undone_at'] else ''}")
        return "\n".join(out)

    def undo(self, changeset: int | None = None, force: bool = False) -> str:
        if changeset is None:
            open_ = [c for c in self.api.history(50) if not c["undone_at"]]
            if not open_:
                return "There is nothing to undo."
            target = open_[0]
        else:
            target = next((c for c in self.api.history(200) if c["id"] == changeset), {"id": changeset, "summary": ""})
        try:
            result = self.api.undo(target["id"], force=force)
        except StashError as e:
            if e.status == 409 and not force:
                return (f"Change #{target['id']} cannot be undone safely: {e.code}. Later changes touched the same "
                        "bookmarks; undo with force=true only if the user agrees to lose those too.")
            raise
        self.ensure(force=True)
        return (f"Undid #{target['id']} {target.get('summary', '')}: {result['restored']} restored, "
                f"{result['removed']} removed.")
