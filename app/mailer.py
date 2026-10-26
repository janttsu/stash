"""E-mail from Stash: the password reset link, and nothing else.

Mail goes straight from this server to the recipient's mail server (port 25, STARTTLS when offered), signed with
DKIM. The domain of STASH_MAIL_FROM needs the SPF and DKIM records that `python -m app.cli mail-dns` prints;
without them some providers refuse the mail.

  STASH_MAIL=dry       write the messages to data/outbox/ instead of sending them (tests, or no outgoing port 25)
  STASH_MAIL_FROM      the sender (default "Stash <noreply@HOST>", HOST being the host name of STASH_ORIGIN)
  STASH_MAIL_HELO      the name this server introduces itself with (default the host name of STASH_ORIGIN)
  STASH_DKIM_KEY       the private DKIM key, PEM (default data/dkim.key, created on first use)
  STASH_DKIM_SELECTOR  the DKIM selector (default "stash")

Another program on the same server that already sends mail with DKIM can be reused: point STASH_DKIM_KEY and
STASH_DKIM_SELECTOR at its key and selector and STASH_MAIL_FROM at an address of its domain, and no new DNS
records are needed.
"""
from __future__ import annotations

import base64
import email.utils
import logging
import os
import smtplib
import ssl
import threading
import time
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from urllib.parse import urlsplit

from . import db

log = logging.getLogger(__name__)

DRY = os.environ.get("STASH_MAIL", "").strip().lower() == "dry"
HOST = urlsplit(os.environ.get("STASH_ORIGIN", "")).hostname or "localhost"
FROM = os.environ.get("STASH_MAIL_FROM", "").strip() or f"Stash <noreply@{HOST}>"
HELO = os.environ.get("STASH_MAIL_HELO", "").strip() or HOST
DKIM_KEY = os.environ.get("STASH_DKIM_KEY", "").strip()
SELECTOR = os.environ.get("STASH_DKIM_SELECTOR", "").strip() or "stash"
RETRY_SECONDS = (60, 300)     # after a failed delivery: try again after one minute, then after five

TEXTS = {
    "en": ("Reset your Stash password",
           "Someone asked to reset the password of the Stash account for this address.\n\n"
           "Open this link within an hour to choose a new password:\n{link}\n\n"
           "If you did not ask, ignore this message: your password stays the same.\n"),
    "fi": ("Palauta Stashin salasana",
           "Joku pyysi salasanan palauttamista tähän osoitteeseen liitetylle Stash-tunnukselle.\n\n"
           "Avaa tunnin kuluessa tämä linkki ja valitse uusi salasana:\n{link}\n\n"
           "Jos et pyytänyt, jätä viesti huomiotta: salasanasi pysyy samana.\n"),
    "sv": ("Återställ ditt Stash-lösenord",
           "Någon bad om att återställa lösenordet för Stash-kontot med den här adressen.\n\n"
           "Öppna den här länken inom en timme för att välja ett nytt lösenord:\n{link}\n\n"
           "Om du inte bad om det kan du ignorera meddelandet: ditt lösenord förblir detsamma.\n"),
}


def dkim_key_path():
    return Path(DKIM_KEY) if DKIM_KEY else db.DATA / "dkim.key"


def dkim_private_key() -> bytes:
    p = dkim_key_path()
    # a key given with STASH_DKIM_KEY belongs to someone else: a new one in its place would not match the DNS
    if not p.exists() and not DKIM_KEY:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        p.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                        serialization.NoEncryption()))
        p.chmod(0o600)
    return p.read_bytes()


def from_domain() -> str:
    _, addr = email.utils.parseaddr(FROM)
    return addr.rsplit("@", 1)[-1] if "@" in addr else HELO


def dkim_dns_record() -> tuple[str, str]:
    """(name, TXT value) of the public DKIM key, for the DNS zone of the sender's domain."""
    from cryptography.hazmat.primitives import serialization

    key = serialization.load_pem_private_key(dkim_private_key(), password=None)
    pub = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return f"{SELECTOR}._domainkey.{from_domain()}", f"v=DKIM1; k=rsa; p={base64.b64encode(pub).decode()}"


def reset_message(to: str, lang: str, link: str) -> EmailMessage:
    subject, body = TEXTS.get(lang, TEXTS["en"])
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = FROM
    msg["To"] = to
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=from_domain())
    msg["Auto-Submitted"] = "auto-generated"
    msg.set_content(body.format(link=link))
    return msg


def _dkim_sign(raw: bytes) -> bytes:
    import dkim

    sig = dkim.sign(raw, SELECTOR.encode(), from_domain().encode(), dkim_private_key(),
                    include_headers=[b"from", b"to", b"subject", b"date", b"message-id"],
                    canonicalize=(b"relaxed", b"relaxed"))
    return sig + raw


def _send_direct(msg: EmailMessage, to: str) -> None:
    import dns.resolver

    domain = to.rsplit("@", 1)[1].lower()
    try:
        mxs = sorted((r.preference, str(r.exchange).rstrip(".")) for r in dns.resolver.resolve(domain, "MX"))
    except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
        mxs = [(0, domain)]
    raw = _dkim_sign(msg.as_bytes())
    last: Exception | None = None
    ctx = ssl.create_default_context()
    for _, host in mxs:
        try:
            with smtplib.SMTP(host, 25, timeout=30, local_hostname=HELO) as s:
                s.ehlo()
                if s.has_extn("starttls"):
                    s.starttls(context=ctx)
                    s.ehlo()
                s.sendmail(email.utils.parseaddr(FROM)[1], [to], raw)
                return
        except (smtplib.SMTPException, OSError) as e:
            last = e
    raise RuntimeError(f"no mail server took the message for {domain}: {last}")


def _write_outbox(msg: EmailMessage) -> None:
    outbox = db.DATA / "outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    (outbox / f"{time.time_ns()}.eml").write_bytes(msg.as_bytes())


def _deliver(msg: EmailMessage, to: str) -> None:
    for attempt in range(len(RETRY_SECONDS) + 1):
        try:
            _send_direct(msg, to)
            return
        except Exception as e:  # noqa: BLE001  (a failing mail never breaks the request that asked for it)
            if attempt == len(RETRY_SECONDS):
                log.warning("mail to %s not delivered: %s", to.split("@")[-1], e)
                return
            time.sleep(RETRY_SECONDS[attempt])


def send(msg: EmailMessage, to: str) -> None:
    """Send in the background (a request does not wait for the recipient's server), or to the outbox in dry mode."""
    if DRY:
        _write_outbox(msg)
        return
    threading.Thread(target=_deliver, args=(msg, to), daemon=True).start()
