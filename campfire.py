"""Import of Campfire meetups for the admin area.

The admin pastes a meetup link (cmpf.re short link or the full Campfire web
URL). The server follows the link to the public meetup page and reads the
link-preview data (Open Graph tags): title, description and cover image.
Date, time and location are not part of that data; they are read from a
screenshot of the meetup in the Campfire app (see below) or entered by hand.

Only the Campfire hosts below are contacted, including redirects.
"""

from __future__ import annotations

import html
import re
import tempfile
import threading
import urllib.error
import urllib.request
from datetime import date as datetime_date, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

ALLOWED_HOSTS = ("cmpf.re", "campfire.scopely.com", "campfire.nianticlabs.com")
MEETUP_PATH = re.compile(r"^/discover/meetup/([0-9a-fA-F-]{36})/?$")
URL_IN_TEXT = re.compile(r"https?://[^\s<>\"']+")
MAX_BYTES = 2 * 1024 * 1024
USER_AGENT = "Mozilla/5.0 (compatible; pogo-kalender; +https://github.com/EmreWest/pogo-kalender)"

OCR_LOCK = threading.Lock()  # one screenshot at a time keeps the server responsive

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


# ---------------------------------------------------------------------------
# Screenshots of the meetup view in the Campfire app
#
# The public meetup page does not contain date, time or location, so the admin
# can upload a screenshot of the meetup in the Campfire app instead. Tesseract
# reads it locally; the image is not stored.

MONTHS = {
    "jan": 1, "januar": 1, "january": 1, "jän": 1,
    "feb": 2, "februar": 2, "february": 2,
    "mär": 3, "märz": 3, "mar": 3, "march": 3, "maerz": 3,
    "apr": 4, "april": 4,
    "mai": 5, "may": 5,
    "jun": 6, "juni": 6, "june": 6,
    "jul": 7, "juli": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "okt": 10, "oktober": 10, "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dez": 12, "dezember": 12, "dec": 12, "december": 12,
}
_MONTH = r"([A-Za-zÄÖÜäöü]{3,9})\.?"
DATE_DE = re.compile(rf"\b(\d{{1,2}})\.\s*{_MONTH}\s+(\d{{4}})\b")
DATE_EN = re.compile(rf"\b{_MONTH}\s+(\d{{1,2}}),?\s+(\d{{4}})\b")
TIME_RANGE = re.compile(
    r"\b(\d{1,2})[:.](\d{2})\s*([AaPp]\.?\s?[Mm]\.?)?\s*[-–—]\s*(\d{1,2})[:.](\d{2})\s*([AaPp]\.?\s?[Mm]\.?)?"
)
COORDINATES = re.compile(r"^\s*-?\d{1,2}[.,]\d{4,}\s*,\s*-?\d{1,3}[.,]\d{4,}\s*$")
STOP_WORDS = re.compile(r"^\W{0,3}\w{0,3}\W{0,3}\s*(Host|Gastgeber|Gruppe|Group|Nimmt teil|Going|Teilnehmer|Kommentare|Comments)\b", re.I)
NOISE = re.compile(r"\s*\b(Bearbeiten|Edit)\b\s*", re.I)
MAX_IMAGE_BYTES = 15 * 1024 * 1024


def _month(name: str) -> int | None:
    key = name.casefold().rstrip(".")
    return MONTHS.get(key) or MONTHS.get(key[:3])


def _find_date(line: str) -> tuple[int, int, int] | None:
    for pattern, order in ((DATE_DE, "dmy"), (DATE_EN, "mdy")):
        match = pattern.search(line)
        if not match:
            continue
        if order == "dmy":
            day, month, year = int(match.group(1)), _month(match.group(2)), int(match.group(3))
        else:
            month, day, year = _month(match.group(1)), int(match.group(2)), int(match.group(3))
        if month and 1 <= day <= 31:
            return year, month, day
    return None


def _clock(hour: str, minute: str, meridiem: str | None) -> tuple[int, int] | None:
    h, m = int(hour), int(minute)
    if meridiem:
        pm = meridiem.strip().casefold().startswith("p")
        if not 1 <= h <= 12:
            return None
        h = (h % 12) + (12 if pm else 0)
    return (h, m) if 0 <= h <= 23 and 0 <= m <= 59 else None


def _strip_icon(line: str) -> str:
    # Icons in front of a line come out as "©)", "@" or similar.
    line = re.sub(r"^[^\wÄÖÜäöüß(\"„]+", "", line)
    line = re.sub(r"^(?:[©®@]|\W)\)?\s+", "", line)
    return line.strip()


def _clean_location(parts: list[str]) -> str:
    text = " ".join(NOISE.sub(" ", part) for part in parts)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s*,\s*", ", ", text)
    text = re.sub(r"(,\s*)+", ", ", text)
    return text.strip(" ,.;:|")[:300]


def _blocks(lines: list[str]) -> list[list[str]]:
    blocks: list[list[str]] = [[]]
    for line in lines:
        if line.strip():
            blocks[-1].append(line.strip())
        elif blocks[-1]:
            blocks.append([])
    return [block for block in blocks if block]


def parse_screenshot_text(text: str) -> dict[str, Any]:
    """Extract title, description, date, time and location from the OCR text."""
    lines = [line.rstrip() for line in str(text or "").splitlines()]
    result: dict[str, Any] = {"title": "", "description": "", "start": None, "end": None, "location": "", "coordinates": None}

    date_index = None
    found_date = None
    for index, line in enumerate(lines):
        found_date = _find_date(line)
        if found_date:
            date_index = index
            break
    if date_index is None:
        return result

    # Title and description: text blocks above the date.
    blocks = _blocks(lines[:date_index])
    if blocks:
        title = " ".join(re.sub(r"[\s:;|]+$", "", line) for line in blocks[0])
        # The "⋮" menu next to the title is sometimes read as ":" or "|".
        result["title"] = re.sub(r"[\s:;|]+$", "", title)[:200]
        result["description"] = "\n".join(" ".join(block) for block in blocks[1:])[:5000]

    # Time range (same line as the date or right below) and an optional end date.
    time_index = None
    times = None
    end_date = None
    for index in range(date_index, min(date_index + 4, len(lines))):
        if index > date_index:
            other = _find_date(lines[index])
            if other and other != found_date and end_date is None:
                end_date = other
        match = TIME_RANGE.search(lines[index])
        if match:
            start = _clock(match.group(1), match.group(2), match.group(3) or match.group(6))
            end = _clock(match.group(4), match.group(5), match.group(6))
            if start and end:
                times = (start, end)
                time_index = index
                break

    year, month, day = found_date
    try:
        start_day = datetime_date(year, month, day)
    except ValueError:
        return result
    if times:
        (sh, sm), (eh, em) = times
        finish_day = start_day
        if end_date:
            try:
                finish_day = datetime_date(*end_date)
            except ValueError:
                finish_day = start_day
        elif (eh, em) <= (sh, sm):
            finish_day = start_day + timedelta(days=1)
        result["start"] = f"{start_day.isoformat()}T{sh:02d}:{sm:02d}"
        result["end"] = f"{finish_day.isoformat()}T{eh:02d}:{em:02d}"
    else:
        result["start"] = start_day.isoformat()
        result["end"] = (datetime_date(*end_date) if end_date else start_day).isoformat()
        result["all_day"] = True

    # Location: lines after the time until the coordinates or the next section.
    location: list[str] = []
    begin = (time_index if time_index is not None else date_index) + 1
    for line in [line for line in lines[begin:] if line.strip()][:8]:
        clean = line.strip()
        if COORDINATES.match(clean):
            lat, lon = (part.strip().replace(",", ".") for part in re.split(r",\s*(?=-?\d{1,3}[.,]\d)", clean, maxsplit=1))
            try:
                result["coordinates"] = [float(lat), float(lon)]
            except ValueError:
                pass
            break
        if STOP_WORDS.match(clean):
            break
        stripped = re.sub(r"[\s.;:|]+$", "", NOISE.sub(" ", _strip_icon(clean)).strip())
        if stripped and not TIME_RANGE.search(stripped) and not _find_date(stripped):
            location.append(stripped)
    result["location"] = _clean_location(location)
    return result


def _prepare_screenshot(source: Path, target: Path) -> None:
    from PIL import Image, ImageChops

    with Image.open(source) as original:
        image = original.convert("RGB")
    width, height = image.size
    # Phone screenshots start with a map. The white sheet below starts with a
    # fully white row; everything above it is cut off.
    gray = image.convert("L")
    if height > width * 1.3:
        limit = int(height * 0.45)
        white = gray.point(lambda value: 255 if value >= 246 else 0)
        for y in range(int(height * 0.08), limit):
            row = white.crop((0, y, width, y + 1))
            if row.getextrema() == (255, 255):
                gray = gray.crop((0, y, width, height))
                image = image.crop((0, y, width, height))
                break
    # Remove blue links and buttons ("Bearbeiten", "Teilnehmen").
    red, _green, blue = image.split()
    bluish = ImageChops.subtract(blue, red).point(lambda value: 255 if value > 60 else 0)
    bright = blue.point(lambda value: 255 if value > 150 else 0)
    mask = ImageChops.multiply(bluish, bright)
    gray.paste(255, mask=mask)
    gray.save(target, format="PNG")


def read_screenshot(data: bytes, languages: str = "deu", timeout: int = 60) -> dict[str, Any]:
    """OCR a screenshot of a Campfire meetup and return the recognised fields."""
    import ocr

    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("Der Screenshot ist zu groß (höchstens 15 MB).")
    try:
        from PIL import Image
    except Exception as exc:  # pragma: no cover - Pillow is part of the image
        raise RuntimeError("Pillow ist nicht installiert.") from exc
    if not ocr.tesseract_available():
        raise RuntimeError("Die Texterkennung (Tesseract) ist nicht installiert.")
    with tempfile.TemporaryDirectory(prefix="campfire-") as directory:
        source = Path(directory) / "upload"
        source.write_bytes(data)
        try:
            with Image.open(source) as image:
                if image.format not in ("PNG", "JPEG", "WEBP"):
                    raise ValueError("Bitte einen Screenshot als PNG, JPEG oder WebP hochladen.")
                if image.width * image.height > 40_000_000:
                    raise ValueError("Der Screenshot hat zu viele Pixel.")
                image.verify()
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("Die Datei ist kein lesbares Bild.") from exc
        prepared = Path(directory) / "prepared.png"
        _prepare_screenshot(source, prepared)
        with OCR_LOCK:
            text = ocr._run_tesseract(prepared, languages, timeout, psm=4)
    return parse_screenshot_text(text)
