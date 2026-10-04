"""Admin commands for the server shell:  .venv/bin/python -m app.cli <command>

  invite                      print a one-time invitation link (needed for the very first account too)
  users                       list accounts
  reset-password <username>   set a new random password (also turns off 2FA, signs the user out)
  make-admin <username>       grant administrator rights
  registration <open|invite|closed>
                              who can create an account (ignored while STASH_REGISTRATION is set)
"""
from __future__ import annotations

import os
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
    elif cmd == "make-admin" and len(args) == 1:
        if not con.execute("UPDATE users SET is_admin=1 WHERE username=?", (args[0],)).rowcount:
            print("no such user", file=sys.stderr)
            return 1
    elif cmd == "registration" and args[:1] in (["open"], ["invite"], ["closed"]):
        db.set_config(con, "registration", args[0])
        if fixed := os.environ.get("STASH_REGISTRATION"):
            print(f"note: STASH_REGISTRATION={fixed} is set; the server uses it until it is removed", file=sys.stderr)
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
