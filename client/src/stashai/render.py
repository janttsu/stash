"""Plain-text views of a proposal's preview and of a set, shared by the TUI and `stashai ask`."""
from __future__ import annotations

from stashai.store import Store, short_url


def tag_change(before: list[str], after: list[str]) -> str:
    added = [t for t in after if t not in before]
    removed = [t for t in before if t not in after]
    return " ".join([*(f"+{t}" for t in added), *(f"-{t}" for t in removed)])


def diff_line(e: dict) -> str:
    head = f"#{e['id']} {e['title'][:70]} ({short_url(e['url'], 40)})"
    if e["change"] == "deleted":
        return f"DELETE  {head}  [{', '.join(e.get('tags', []))}]  {e['location'][0]}"
    if e["change"] == "created":
        return f"ADD     {e['title'][:70]} ({short_url(e['url'], 50)})  [{', '.join(e.get('tags', []))}]  → {e['location'][1]}"
    parts = []
    if "tags" in e:
        parts.append("tags " + tag_change(*e["tags"]))
    if "location" in e:
        parts.append(f"{e['location'][0]} → {e['location'][1]}")
    for field in ("title", "url", "notes"):
        if field in e:
            parts.append(f"{field}: {str(e[field][0])[:40]!r} → {str(e[field][1])[:60]!r}")
    if "color" in e:
        parts.append(f"color {e['color'][0] or '-'} → {e['color'][1] or '-'}")
    return f"CHANGE  {head}  " + "; ".join(parts)


def plan_text(summary: str, preview: dict, *, limit: int | None = None) -> list[str]:
    c = preview["counts"]
    head = [f"PROPOSAL: {summary}",
            f"  {c['updated']} changed, {c['deleted']} deleted, {c['created']} added"]
    if preview.get("created_categories"):
        head[-1] += f", {len(preview['created_categories'])} new categories"
    for s in preview.get("skipped", []):
        if s["reason"] == "exists":
            head.append(f"  skipped, already bookmarked: {s['url']}")
        else:
            head.append(f"  skipped: {s['count']} bookmarks not found")
    entries = sorted(preview["diff"], key=lambda e: ({"deleted": 0, "updated": 1, "created": 2}[e["change"]], e["id"]))
    lines = [diff_line(e) for e in entries]
    if limit is not None and len(lines) > limit:
        lines = lines[:limit] + [f"… and {len(lines) - limit} more"]
    return head + [""] + lines


def set_text(store: Store, name: str) -> list[str]:
    ids = store.resolve(name)
    label = store.sets[name].label if name in store.sets else "every bookmark"
    return [f"{name}: {len(ids)} bookmarks – {label}", ""] + [store.line(i, notes=False) for i in ids]
