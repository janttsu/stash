"""Outbound HTTP for dead-link checks, page titles and favicons.

Every request is made by users' bookmark URLs, so each hop is resolved first and
refused unless the address is public (no loopback/private/link-local targets).
The connection then goes to that exact IP, with Host/SNI set to the real name,
so a second DNS answer cannot redirect it elsewhere.
"""
from __future__ import annotations

import asyncio
import html
import ipaddress
import re
import socket
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

UA = "Mozilla/5.0 (compatible; StashBot/1.0)"
REDIRECTS = (301, 302, 303, 307, 308)


class FetchError(Exception):
    pass


class NoSuchHost(FetchError):
    pass


def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(12, connect=8),
        follow_redirects=False,
        limits=httpx.Limits(max_connections=40, max_keepalive_connections=0),
        headers={"User-Agent": UA, "Accept": "*/*", "Accept-Language": "en,fi;q=0.8"},
    )


async def resolve_public(host: str, port: int) -> str:
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        if e.errno in (socket.EAI_NONAME, getattr(socket, "EAI_NODATA", socket.EAI_NONAME)):
            raise NoSuchHost(host) from e
        raise FetchError(f"dns: {e}") from e
    except UnicodeError as e:
        raise FetchError("bad hostname") from e
    # IPv4 first: the server may not have working IPv6 routing
    for family, *_rest, sockaddr in sorted(infos, key=lambda i: i[0] != socket.AF_INET):
        ip = ipaddress.ip_address(sockaddr[0])
        if ip.is_global and not ip.is_multicast:
            return str(ip)
    raise FetchError("address not allowed")


async def fetch(client: httpx.AsyncClient, url: str, *, method: str = "GET", max_bytes: int = 0,
                max_redirects: int = 5, user_agent: str | None = None) -> tuple[int, httpx.Headers, bytes, str]:
    """Return (status, headers, body[:max_bytes], final_url)."""
    for _ in range(max_redirects + 1):
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise FetchError("unsupported url")
        try:
            host = parts.hostname.encode("idna").decode()
            port = parts.port
        except (UnicodeError, ValueError) as e:
            raise FetchError("bad hostname") from e
        ip = await resolve_public(host, port or (443 if parts.scheme == "https" else 80))
        netloc = f"[{ip}]" if ":" in ip else ip
        host_header = host
        if port:
            netloc += f":{port}"
            host_header += f":{port}"
        target = urlunsplit((parts.scheme, netloc, parts.path or "/", parts.query, ""))
        try:
            headers = {"Host": host_header}
            if user_agent:
                headers["User-Agent"] = user_agent
            async with client.stream(method, target, headers=headers, extensions={"sni_hostname": host}) as r:
                if r.status_code in REDIRECTS and r.headers.get("location"):
                    url = urljoin(url, r.headers["location"])
                    continue
                body = b""
                if max_bytes and method != "HEAD":
                    async for chunk in r.aiter_bytes():
                        body += chunk
                        if len(body) >= max_bytes:
                            break
                return r.status_code, r.headers, body[:max_bytes], url
        except httpx.HTTPError as e:
            raise FetchError(type(e).__name__) from e
    raise FetchError("too many redirects")


# These answer 404 to anyone who is not signed in (private documents, repositories, boards),
# so a 404 from them says nothing about whether the bookmark still works.
LOGIN_GATED = ("google.com", "github.com", "gitlab.com", "bitbucket.org", "atlassian.net", "sharepoint.com",
               "trello.com", "zendesk.com", "facebook.com", "linkedin.com", "instagram.com", "office.com",
               "live.com", "dropbox.com", "notion.so", "notion.com")
# Names that only resolve (or only answer) inside a company or home network
_PRIVATE_NAME = re.compile(r"(^|\.)(intra|intranet|internal|corp|lan|local|home)(\.|$)")
BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36"


def login_gated(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in LOGIN_GATED) or bool(_PRIVATE_NAME.search(host))


async def is_dead(client: httpx.AsyncClient, url: str) -> bool:
    """Dead = the domain does not exist, or the page answers 404/410."""
    if login_gated(url):
        return False
    try:
        try:
            status, *_ = await fetch(client, url, method="HEAD")
        except NoSuchHost:
            raise
        except FetchError:
            status = 0
        if status in (404, 410) or status == 0 or status == 405:
            # plenty of servers mishandle HEAD; only a GET result counts
            status, *_ = await fetch(client, url, method="GET")
        if status in (404, 410):
            # some sites answer 404 to anything that announces itself as a bot
            status, *_ = await fetch(client, url, method="GET", user_agent=BROWSER_UA)
        return status in (404, 410)
    except NoSuchHost:
        return True
    except FetchError:
        return False


_TITLE_RE = re.compile(rb"<title[^>]*>(.*?)</title", re.I | re.S)
_CHARSET_RE = re.compile(rb"charset=[\"']?([\w-]+)", re.I)


async def page_title(client: httpx.AsyncClient, url: str) -> str:
    status, headers, body, _ = await fetch(client, url, max_bytes=262144)
    m = _TITLE_RE.search(body)
    if not m:
        return ""
    cs = _CHARSET_RE.search(headers.get("content-type", "").encode()) or _CHARSET_RE.search(body[:4096])
    try:
        text = m.group(1).decode(cs.group(1).decode() if cs else "utf-8", "replace")
    except LookupError:
        text = m.group(1).decode("utf-8", "replace")
    return " ".join(html.unescape(text).split())[:500]


def sniff_image(data: bytes) -> str | None:
    """Content type of a raster image we are willing to serve from our own origin (no SVG)."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith((b"\x00\x00\x01\x00", b"\x00\x00\x02\x00")):
        return "image/x-icon"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


_LINK_RE = re.compile(rb"<link\b[^>]*>", re.I)
_REL_ICON_RE = re.compile(rb"rel\s*=\s*[\"']?[^\"'>]*\bicon\b", re.I)
_HREF_RE = re.compile(rb"href\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))", re.I)
MAX_ICON = 300_000


async def favicon(client: httpx.AsyncClient, host: str) -> bytes | None:
    async def get(url: str) -> bytes | None:
        try:
            status, _, body, _ = await fetch(client, url, max_bytes=MAX_ICON + 1)
        except FetchError:
            return None
        return body if status == 200 and len(body) <= MAX_ICON and sniff_image(body) else None

    icon = await get(f"https://{host}/favicon.ico")
    if icon:
        return icon
    try:
        status, _, page, final = await fetch(client, f"https://{host}/", max_bytes=200_000)
    except FetchError:
        return None
    if status != 200:
        return None
    for tag in _LINK_RE.findall(page)[:60]:
        if not _REL_ICON_RE.search(tag):
            continue
        m = _HREF_RE.search(tag)
        if not m:
            continue
        href = html.unescape((m.group(1) or m.group(2) or m.group(3)).decode("utf-8", "replace")).strip()
        if href.startswith("data:"):
            continue
        icon = await get(urljoin(final, href))
        if icon:
            return icon
    return None
