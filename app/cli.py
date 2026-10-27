"""Admin commands for the server shell:  .venv/bin/python -m app.cli <command>

  invite                      print a one-time invitation link (needed for the very first account too)
  users                       list accounts
  reset-password <username>   set a new random password (also turns off 2FA, signs the user out)
  make-admin <username>       grant administrator rights
  set-email <username> <address>
                              the address for password reset links ("-" removes it)
  mail-dns                    the DNS records the sending domain needs (SPF and DKIM) for the mail of reset links
  registration <open|invite|closed>
                              who can create an account (ignored while STASH_REGISTRATION is set)
  job <media-sync|duplicates> run a scheduled task now (timers run them; see app/jobs.py)
  jobs                        when each scheduled task last ran, and whether it is overdue
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys

from . import db, security


def main(argv: list[str]) -> int:
    db.init()
    con = db.connect()
    cmd, args = (argv[0] if argv else ""), argv[1:]
    if cmd == "invite":
        code = security.new_token(12)
        con.execute("INSERT INTO invites(code, created_at) VALUES(?, ?)", (code, db.now()))
        print(f"{os.environ.get('STASH_ORIGIN', 'http://localhost:8003')}/?invite={code}")
    elif cmd == "users":
        for r in con.execute("SELECT u.id, u.username, u.is_admin, u.totp_on,"
                             " (SELECT COUNT(*) FROM bookmarks b WHERE b.user_id=u.id) AS n FROM users u ORDER BY u.id"):
            flags = ", ".join(f for f, on in (("admin", r["is_admin"]), ("2fa", r["totp_on"])) if on)
            print(f"{r['id']:>4}  {r['username']:<32} {r['n']:>6} bookmarks  {flags}")
    elif cmd == "reset-password" and len(args) == 1:
        password = security.new_token(9)
        with db.tx(con):
            cur = con.execute("UPDATE users SET pw_hash=?, totp_on=0, totp_secret=NULL WHERE username=?",
                              (security.hash_password(password), args[0]))
            if not cur.rowcount:
                print("no such user", file=sys.stderr)
                return 1
            con.execute("DELETE FROM sessions WHERE user_id=(SELECT id FROM users WHERE username=?)", (args[0],))
        print(f"new password for {args[0]}: {password}")
    elif cmd == "set-email" and len(args) == 2:
        address = "" if args[1] == "-" else args[1].strip()
        if address and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", address):
            print("not an e-mail address", file=sys.stderr)
            return 1
        try:
            if not con.execute("UPDATE users SET email=? WHERE username=?", (address, args[0])).rowcount:
                print("no such user", file=sys.stderr)
                return 1
        except sqlite3.IntegrityError:
            print("another account already has this address", file=sys.stderr)
            return 1
        print(f"{args[0]}: {address or 'no address'}")
    elif cmd == "mail-dns":
        from . import mailer
        name, value = mailer.dkim_dns_record()
        domain = mailer.from_domain()
        print(f"Records for {domain} (the host name is relative to the zone of {domain}):\n")
        print(f"SPF    TXT  {domain}  v=spf1 ip4:<this server's public IPv4> -all")
        print(f"DKIM   TXT  {name}  {value}")
        print(f"DMARC  TXT  _dmarc.{domain}  v=DMARC1; p=none; rua=mailto:<your address>")
    elif cmd == "make-admin" and len(args) == 1:
        if not con.execute("UPDATE users SET is_admin=1 WHERE username=?", (args[0],)).rowcount:
            print("no such user", file=sys.stderr)
            return 1
    elif cmd == "registration" and args[:1] in (["open"], ["invite"], ["closed"]):
        db.set_config(con, "registration", args[0])
        if fixed := os.environ.get("STASH_REGISTRATION"):
            print(f"note: STASH_REGISTRATION={fixed} is set; the server uses it until it is removed", file=sys.stderr)
    elif cmd == "job" and len(args) == 1 and args[0] in ("media-sync", "duplicates"):
        from . import jobs

        con.close()
        result = jobs.RUN[args[0]]()
        print(f"{args[0]}: {'ok' if result['ok'] else 'FAILED'}: {result['summary']}")
        return 0 if result["ok"] else 1
    elif cmd == "jobs":
        import time

        from . import jobs

        for j in jobs.status(con):
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(j["last_ok"])) if j["last_ok"] else "never"
            print(f"{j['name']:<12} {'OVERDUE' if j['overdue'] else 'ok':<8} last ok {when}  {j['summary'][:150]}")
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
