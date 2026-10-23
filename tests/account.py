"""Addresses on accounts and password reset by e-mail, against a throwaway database:  .venv/bin/python tests/account.py

Mail goes to data/outbox in this test (STASH_MAIL=dry), so the reset link can be read from the message.
"""
import email
import email.policy
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["STASH_DATA"] = tempfile.mkdtemp(prefix="stash-test-")
os.environ["STASH_INSECURE_COOKIE"] = "1"
os.environ["STASH_MAINTENANCE"] = "off"
os.environ["STASH_MAIL"] = "dry"

from fastapi.testclient import TestClient  # noqa: E402

from app import cli, db, mailer, security  # noqa: E402
from app.main import app  # noqa: E402

OUTBOX = db.DATA / "outbox"


def ok(r, status=200):
    assert r.status_code == status, f"{r.request.method} {r.request.url} -> {r.status_code} {r.text}"
    return r.json()


def fail(r, status, code):
    assert r.status_code == status and r.json().get("detail") == code, f"{r.status_code} {r.text}, wanted {code}"


def mails():
    return sorted(OUTBOX.glob("*.eml")) if OUTBOX.exists() else []


def last_mail():
    return email.message_from_bytes(mails()[-1].read_bytes(), policy=email.policy.default)


def last_token():
    m = re.search(r"#reset=([\w-]+)", last_mail().get_body().get_content())
    assert m, "the mail carries a reset link"
    return m.group(1)


with TestClient(app):
    web = TestClient(app, headers={"X-Stash": "1"})
    cli.main(["invite"])
    code = db.connect().execute("SELECT code FROM invites").fetchone()["code"]
    ok(web.post("/api/register", json={"username": "alice", "password": "correct horse", "invite": code,
                                       "email": "Alice@Example.com"}))
    assert ok(web.get("/api/me"))["email"] == "Alice@Example.com", "the address is kept as typed"
    ok(web.put("/api/admin/config", json={"registration": "open"}))

    bob = TestClient(app, headers={"X-Stash": "1"})
    ok(bob.post("/api/register", json={"username": "bob", "password": "bob password"}))   # no address: allowed
    assert ok(bob.get("/api/me"))["email"] == ""

    carol = TestClient(app, headers={"X-Stash": "1"})
    fail(carol.post("/api/register", json={"username": "carol", "password": "carol password",
                                           "email": "alice@example.com"}), 409, "email_taken")
    fail(carol.post("/api/register", json={"username": "carol", "password": "carol password",
                                           "email": "not an address"}), 422, "bad_email")
    ok(carol.post("/api/register", json={"username": "carol", "password": "carol password",
                                         "email": "carol@example.com"}))

    # --- password reset by e-mail
    anon = TestClient(app, headers={"X-Stash": "1"})
    ok(anon.post("/api/password/forgot", json={"email": "alice@example.com", "lang": "fi"}))
    assert len(mails()) == 1, "one mail for the account"
    msg = last_mail()
    assert msg["To"] == "Alice@Example.com" and msg["Subject"] == mailer.TEXTS["fi"][0], "sent to the address, in Finnish"
    assert "Auto-Submitted" in msg and msg["DKIM-Signature"] is None, "the outbox copy is the unsigned message"
    link = last_token()

    ok(anon.post("/api/password/forgot", json={"email": "nobody@example.com"}))
    ok(anon.post("/api/password/forgot", json={"email": "bob@example.com"}))   # bob has no address on his account
    assert len(mails()) == 1, "no mail for unknown addresses or accounts without one"

    old_session = TestClient(app, headers={"X-Stash": "1"})
    ok(old_session.post("/api/login", json={"username": "alice", "password": "correct horse"}))
    fail(anon.post("/api/password/reset", json={"token": "not-a-token", "password": "new password here"}),
         400, "bad_reset_link")
    ok(anon.post("/api/password/reset", json={"token": link, "password": "new password here"}))
    fail(anon.post("/api/login", json={"username": "alice", "password": "correct horse"}), 401, "invalid_credentials")
    ok(anon.post("/api/login", json={"username": "alice", "password": "new password here"}))
    assert old_session.get("/api/me").status_code == 401, "the reset signs out every device"
    fail(anon.post("/api/password/reset", json={"token": link, "password": "another password"}),
         400, "bad_reset_link")   # a link works once

    ok(anon.post("/api/password/forgot", json={"email": "alice@example.com"}))
    expired = last_token()
    db.connect().execute("UPDATE password_resets SET expires_at=0")
    fail(anon.post("/api/password/reset", json={"token": expired, "password": "new password here"}),
         400, "bad_reset_link")

    # --- two-factor stays on: a reset does not switch it off
    dave = TestClient(app, headers={"X-Stash": "1"})
    ok(dave.post("/api/register", json={"username": "dave", "password": "dave password", "email": "dave@example.com"}))
    db.connect().execute("UPDATE users SET totp_on=1, totp_secret=? WHERE username='dave'", (security.new_totp_secret(),))
    ok(anon.post("/api/password/forgot", json={"email": "dave@example.com"}))
    ok(anon.post("/api/password/reset", json={"token": last_token(), "password": "dave new password"}))
    fail(anon.post("/api/login", json={"username": "dave", "password": "dave new password"}), 401, "totp_required")

    # --- changing the address: needs the password, one address per account
    ok(web.post("/api/login", json={"username": "alice", "password": "new password here"}))
    fail(web.post("/api/account/email", json={"password": "wrong password", "email": "x@example.com"}), 403,
         "wrong_password")
    fail(web.post("/api/account/email", json={"password": "new password here", "email": "carol@example.com"}), 409,
         "email_taken")
    fail(web.post("/api/account/email", json={"password": "new password here", "email": "no good"}), 422, "bad_email")
    ok(web.post("/api/account/email", json={"password": "new password here", "email": "alice2@example.com"}))
    assert ok(web.get("/api/me"))["email"] == "alice2@example.com"
    ok(web.post("/api/account/email", json={"password": "new password here", "email": ""}))
    assert ok(web.get("/api/me"))["email"] == "", "an empty address removes it"
    ok(web.post("/api/account/email", json={"password": "new password here", "email": "alice@example.com"}))

    # --- the server command: an address for an account that has none (christian's case)
    assert cli.main(["set-email", "bob", "bob@example.com"]) == 0
    assert cli.main(["set-email", "bob", "carol@example.com"]) == 1, "taken by carol"
    assert cli.main(["set-email", "nobody", "x@example.com"]) == 1
    assert cli.main(["set-email", "bob", "not-an-address"]) == 1
    ok(anon.post("/api/password/forgot", json={"email": "bob@example.com"}))
    assert len(mails()) == 4 and last_mail()["To"] == "bob@example.com", "bob can reset by e-mail now"

    # --- the DNS records of the sending domain
    name, value = mailer.dkim_dns_record()
    assert name.startswith("stash._domainkey.") and value.startswith("v=DKIM1; k=rsa; p=")
    assert mailer._dkim_sign(b"From: a@b.c\r\n\r\nhi\r\n").startswith(b"DKIM-Signature:")

    # --- guessing: a flood of requests for one address is refused
    refused = [anon.post("/api/password/forgot", json={"email": "eve@example.com"}).status_code for _ in range(8)]
    assert 429 in refused, refused

print("all account tests passed")
