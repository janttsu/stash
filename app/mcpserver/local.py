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
from .. import api_v1, db, handover, history, playlists, subscriptions, tabs  # noqa: E402,I001
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
        """The user's standing rules for the assistant (AI → Assistant (MCP))."""
        def read(c: KeyCtx) -> str:
            return str(json.loads(c.user["settings"] or "{}").get("assistant_rules", "")).strip()
        return self._call(read)

    def history(self, limit: int = 20) -> list[dict]:
        return self._call(lambda c: api_v1.changeset_list(c, max(1, min(200, limit))))

    # --- project handovers (app/handover.py): separate from bookmarks ---------------------------------

    def handover_list(self) -> list[dict]:
        return self._call(handover.list_projects)

    def handover_get(self, project: str, version=None) -> dict:
        return self._call(lambda c: handover.get(c, project, version))

    def handover_save(self, project: str, body: str, **fields) -> dict:
        def run(c: KeyCtx):
            self._writer(c)
            return handover.save(c, project, body, **fields)
        return self._call(run)

    # --- open browser tabs sent to Stash (app/tabs.py) ---------------------------------------------------

    def open_tab_lists(self) -> list[dict]:
        return self._call(tabs.lists)

    def open_tabs(self, **spec) -> dict:
        return self._call(lambda c: tabs.tabs_of(c, **spec))

    def open_tabs_resolve(self, ids: list[int], state: str, note=None) -> dict:
        def run(c: KeyCtx):
            self._writer(c)
            return tabs.resolve(c, ids, state, note)
        return self._call(run)

    def open_tabs_mark_saved(self, ids: list[int], note=None) -> dict:
        def run(c: KeyCtx):
            self._writer(c)
            return tabs.mark_saved(c, ids, note)
        return self._call(run)

    # --- playlists from Spotify and YouTube (app/playlists.py) ------------------------------------------

    def _playlist_call(self, fn, *, write: bool):
        def run(c: KeyCtx):
            if write:
                self._writer(c)
            try:
                return fn(c)
            except playlists.ServiceError as e:
                code = playlists.problem_code(c, e)
                if code.startswith("youtube_api_disabled"):
                    project = code.split(":", 1)[1]
                    raise StashError(409, "youtube_api_disabled: the YouTube Data API v3 is not enabled in the user's "
                                          f"Google Cloud project {project}. Ask the user to enable it at "
                                          f"https://console.cloud.google.com/apis/library/youtube.googleapis.com?project={project}"
                                          ", wait a minute and try again.") from e
                if code.startswith("reconnect_needed"):
                    raise StashError(409, f"reconnect_needed: the {e.service} connection lacks a permission Stash now "
                                          "asks for. Ask the user to press “Connect again…” for it on the Playlists page.") from e
                if code.startswith("rate_limited"):
                    import time as _time

                    until = int(code.rsplit(":", 1)[1] or 0)
                    raise StashError(429, f"rate_limited: {e.service} has paused requests from the user's app until "
                                          f"{_time.strftime('%Y-%m-%d %H:%M', _time.localtime(until))}. Do not retry before "
                                          "then; tell the user.") from e
                raise StashError(502, f"{e.service} answered {e.status}: {e.message}") from e
        return self._call(run)

    def playlists_overview(self) -> dict:
        return self._playlist_call(playlists.overview, write=False)

    def playlist_show(self, playlist_id: int, offset: int = 0, limit: int = 500) -> dict:
        return self._playlist_call(lambda c: playlists.show(c, playlist_id, offset, limit), write=False)

    def playlist_edit(self, playlist_id: int, ops: list[dict]) -> dict:
        return self._playlist_call(lambda c: playlists.edit(c, playlist_id, ops), write=True)

    def playlist_discard(self, playlist_id: int) -> dict:
        return self._playlist_call(lambda c: playlists.discard(c, playlist_id), write=True)

    def playlist_push(self, playlist_id: int) -> dict:
        return self._playlist_call(lambda c: playlists.push(c, playlist_id), write=True)

    def playlist_delete(self, playlist_id: int, keep: bool = False) -> dict:
        return self._playlist_call(lambda c: playlists.mark_delete(c, playlist_id, keep), write=True)

    def playlist_publish(self, playlist_id: int) -> dict:
        return self._playlist_call(lambda c: playlists.publish(c, playlist_id), write=True)

    def playlists_sync(self, service: str, force: bool = False, push_pending: bool = False) -> dict:
        if push_pending:
            return self._playlist_call(lambda c: playlists.sync_all(c, service), write=True)
        return self._playlist_call(lambda c: playlists.pull(c, service, None, force), write=True)

    def playlist_create(self, service: str, name: str, description: str = "", public: bool = False) -> dict:
        return self._playlist_call(lambda c: playlists.create(c, service, name, description, public), write=True)

    # --- subscriptions (app/subscriptions.py) ---------------------------------------------------------------

    def subscriptions_overview(self) -> dict:
        return self._playlist_call(subscriptions.overview, write=False)

    def subscriptions_list(self, service: str, **spec) -> dict:
        return self._playlist_call(lambda c: subscriptions.listing(c, service, **spec), write=False)

    def subscriptions_pending(self, service: str) -> dict:
        return self._playlist_call(lambda c: subscriptions.pending(c, service), write=False)

    def subscriptions_edit(self, service: str, ops: list[dict]) -> dict:
        return self._playlist_call(lambda c: subscriptions.edit(c, service, ops), write=True)

    def subscriptions_sync(self, service: str, push: bool = False) -> dict:
        return self._playlist_call(lambda c: subscriptions.sync(c, service, push), write=True)

    def subscriptions_activity(self, service: str, ids: list[int] | None = None) -> dict:
        return self._playlist_call(lambda c: subscriptions.check_activity(c, service, ids), write=True)

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
