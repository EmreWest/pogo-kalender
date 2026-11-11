"""Import of Campfire meetup links for the admin area.

The admin pastes a meetup link (cmpf.re short link or the full Campfire web
URL). The server follows the link to the public meetup page and reads the
link-preview data (Open Graph tags): title, description and cover image.
Date, time and location are not part of that data and are entered by hand.

Only the Campfire hosts below are contacted, including redirects.
"""

from __future__ import annotations

import html
import re
import urllib.error
import urllib.request
from html.parser import HTMLParser
from typing import Any, Callable
from urllib.parse import urlsplit

ALLOWED_HOSTS = ("cmpf.re", "campfire.scopely.com", "campfire.nianticlabs.com")
MEETUP_PATH = re.compile(r"^/discover/meetup/([0-9a-fA-F-]{36})/?$")
URL_IN_TEXT = re.compile(r"https?://[^\s<>\"']+")
MAX_BYTES = 2 * 1024 * 1024
USER_AGENT = "Mozilla/5.0 (compatible; pogo-kalender; +https://github.com/EmreWest/pogo-kalender)"

Fetcher = Callable[[str, int], tuple[str, bytes]]


def host_allowed(url: str) -> bool:
    parts = urlsplit(url)
    host = (parts.hostname or "").casefold()
    return parts.scheme == "https" and host in ALLOWED_HOSTS


def extract_link(text: str) -> str:
    """Find the Campfire link in pasted text (share texts contain more than the URL)."""
    text = str(text or "").strip()
    for candidate in URL_IN_TEXT.findall(text):
        candidate = candidate.rstrip(".,;:!?)]}")
        if host_allowed(candidate):
            return candidate
    if text and "://" not in text and text.split("/", 1)[0].casefold() in ALLOWED_HOSTS:
        return extract_link("https://" + text)
    raise ValueError("Bitte einen Campfire-Link einfügen (cmpf.re/… oder campfire.scopely.com/discover/meetup/…).")


def meetup_id(url: str) -> str | None:
    if not host_allowed(url):
        return None
    match = MEETUP_PATH.match(urlsplit(url).path)
    return match.group(1).lower() if match else None


class _AllowlistRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        if not host_allowed(newurl):
            raise urllib.error.HTTPError(newurl, code, "Weiterleitung auf fremden Host blockiert", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def default_fetch(url: str, timeout: int) -> tuple[str, bytes]:
    opener = urllib.request.build_opener(_AllowlistRedirect())
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "de,en;q=0.8",
    })
    with opener.open(request, timeout=timeout) as response:
        return response.geturl(), response.read(MAX_BYTES)


class _MetaParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
        if tag != "meta":
            return
        values = {key.casefold(): value or "" for key, value in attrs}
        key = (values.get("property") or values.get("name") or "").casefold()
        if key and key not in self.meta and values.get("content"):
            self.meta[key] = values["content"]

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data


def _clean(value: str, limit: int) -> str:
    value = html.unescape(value or "").replace("\r\n", "\n").strip()
    value = re.sub(r"[ \t]+", " ", value)
    return value[:limit]


def parse_meetup_page(page: bytes) -> dict[str, str]:
    parser = _MetaParser()
    parser.feed(page.decode("utf-8", errors="replace"))
    meta = parser.meta
    title = _clean(meta.get("og:title") or meta.get("twitter:title") or parser.title, 200)
    description = _clean(meta.get("og:description") or meta.get("description") or meta.get("twitter:description") or "", 5000)
    image = meta.get("og:image") or meta.get("twitter:image") or ""
    if title.casefold() in ("campfire", "niantic campfire", ""):
        title = ""
    return {"title": title, "description": description, "image": image if image.startswith("https://") else ""}


def fetch_meetup(text: str, timeout: int = 15, fetch: Fetcher | None = None) -> dict[str, Any]:
    """Resolve a pasted Campfire link and return the public meetup preview."""
    link = extract_link(text)
    try:
        final_url, page = (fetch or default_fetch)(link, timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise ValueError("Campfire kennt dieses Meetup nicht (mehr).") from exc
        raise RuntimeError(f"Campfire antwortet mit Fehler {exc.code}.") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"Campfire ist gerade nicht erreichbar: {exc}") from exc
    ident = meetup_id(final_url)
    if not ident:
        raise ValueError("Der Link führt nicht zu einem Campfire-Meetup.")
    data = parse_meetup_page(page)
    if not data["title"]:
        raise ValueError("Auf der Meetup-Seite wurde kein Titel gefunden.")
    return {
        "id": ident,
        "url": f"https://campfire.scopely.com/discover/meetup/{ident}",
        **data,
    }
