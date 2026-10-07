"""The Stash API as the MCP tools use it: the same functions as /api/v1, called in-process for one API key.

Each call opens its own database connection and checks the key again, so a key revoked in Settings stops working
at once, also in the middle of a session.
"""
from __future__ import annotations

import asyncio
import json

import anyio
from fastapi import HTTPException
from pydantic import ValidationError

from .. import main  # noqa: F401  (first: main, api_v1 and history import each other)
from .. import api_v1, db, history  # noqa: E402,I001
from ..api_v1 import KeyCtx

MAX_OPS = 20_000   # one preview may touch many bookmarks (e.g. new titles for a whole set)


class StashError(RuntimeError):
    def __init__(self, status: int, code: str):
        super().__init__(f"Stash {status}: {code}")
        self.status, self.code = status, code


class LocalAPI:
    def __init__(self, key_id: int):
        self.key_id = key_id

    def _ctx(self, con) -> KeyCtx:
        key = con.execute("SELECT * FROM api_keys WHERE id=?", (self.key_id,)).fetchone()
        if not key:
            raise StashError(401, "bad_api_key")
        user = con.execute("SELECT * FROM users WHERE id=?", (key["user_id"],)).fetchone()
        return KeyCtx(con, user, key)

    def _call(self, fn):
        con = db.connect()
        try:
            return fn(self._ctx(con))
        except HTTPException as e:
            raise StashError(e.status_code, str(e.detail)) from e
        finally:
            con.close()

    @staticmethod
    def _writer(c: KeyCtx) -> None:
        if not c.key["can_write"]:
            raise StashError(403, "read_only_key: this API key can only read; give it “Allow changes” in Settings")

    # --- reading ------------------------------------------------------------------------------------

    def snapshot(self) -> dict:
        return self._call(api_v1.snapshot_all)

    def usage(self) -> dict:
        return self._call(history.usage)

    def browsing(self, q=None, host=None, min_visits=1, period="90d", bookmarked="any", limit=50) -> dict:
        return self._call(lambda c: history.search(q=q or "", host=host or "", min_visits=min_visits, period=period,
                                                   bookmarked=bookmarked, sort="visits_90d", offset=0, limit=limit, c=c))

    def rules(self) -> str:
        """The user's standing rules for the assistant (Settings → Assistant (MCP))."""
        def read(c: KeyCtx) -> str:
            return str(json.loads(c.user["settings"] or "{}").get("assistant_rules", "")).strip()
        return self._call(read)

    def history(self, limit: int = 20) -> list[dict]:
        return self._call(lambda c: api_v1.changeset_list(c, max(1, min(200, limit))))

    # --- changing -------------------------------------------------------------------------------------

    def changes(self, ops: list[dict], summary: str, *, dry_run: bool) -> dict:
        if len(ops) > MAX_OPS:
            raise StashError(422, f"too many operations at once ({len(ops)}); narrow the set")
        try:
            body = api_v1.Changes.model_construct(ops=[api_v1.Op(**o) for o in ops], summary=summary[:500],
                                                  dry_run=dry_run)
        except ValidationError as e:
            raise StashError(422, "invalid operation: " + "; ".join(
                f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()[:3])) from e
        return self._call(lambda c: api_v1.post_changes(body, c))

    def undo(self, changeset: int, *, force: bool = False) -> dict:
        def run(c: KeyCtx):
            self._writer(c)
            return api_v1.undo_changeset(c, changeset, force)
        return self._call(run)

    def refresh_icons(self, ids: list[int]) -> dict:
        """Stash fetches the site icons of these bookmarks again (one fetch per site, several sites at once)."""
        def hosts_of(c: KeyCtx) -> list[str]:
            self._writer(c)
            found: set[str] = set()
            for part in db.chunks(ids):
                found |= {r[0] for r in c.con.execute(
                    f"SELECT DISTINCT host FROM bookmarks WHERE user_id=? AND host<>'' AND id IN ({db.marks(len(part))})",
                    [c.uid, *part])}
            return sorted(found)

        hosts = self._call(hosts_of)

        async def fetch_all(client) -> dict[str, str]:
            from ..main import refresh_favicon

            sem = asyncio.Semaphore(12)

            async def one(host: str) -> tuple[str, str]:
                async with sem:
                    return host, await refresh_favicon(client, host)

            return dict(await asyncio.gather(*(one(h) for h in hosts)))

        async def own_client() -> dict[str, str]:
            from .. import net

            async with net.make_client() as client:
                return await fetch_all(client)

        results: dict[str, str] = {}
        if hosts:
            from ..main import app

            try:  # inside the server: use its event loop and HTTP client
                results = anyio.from_thread.run(fetch_all, app.state.http)
            except (RuntimeError, AttributeError):  # called outside the server's worker threads (tests, scripts)
                results = asyncio.run(own_client())
        if "new" in results.values():
            con = db.connect()
            try:
                db.set_config(con, "favicon_rev", str(db.now()))
            finally:
                con.close()
        counts = {k: sum(v == k for v in results.values()) for k in ("new", "same", "kept", "missing")}
        return {"sites": results, "counts": counts}
