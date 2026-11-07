"""Location search by German postal code and/or place name.

German postcodes are searched offline in the bundled GeoNames extract
``plz-de.json`` (CC BY 4.0, www.geonames.org), so typing a postcode never
leaves the server. Place names that are not found there (e.g. Salzburg) fall
back to the Open-Meteo geocoding API.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import unicodedata
import urllib.parse
from pathlib import Path
from typing import Any, Callable

LOGGER = logging.getLogger("scrapedduck-ical.locations")
MAX_RESULTS = 25


def fold(text: str) -> str:
    """Case- and accent-insensitive form: "Fürth" == "furth" == "fuerth"."""
    value = str(text or "").casefold().replace("ß", "ss")
    for source, target in (("ä", "ae"), ("ö", "oe"), ("ü", "ue")):
        value = value.replace(source, target)
    value = unicodedata.normalize("NFKD", value)
    value = "".join(char for char in value if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def parse_query(query: str) -> tuple[str, str]:
    """Split "34117 Kassel" into postcode prefix and place text."""
    text = " ".join(str(query or "").split())[:80]
    digits = re.findall(r"\b\d{1,5}\b", text)
    postcode = digits[0] if digits else ""
    place = re.sub(r"\b\d{1,5}\b", " ", text)
    return postcode, " ".join(place.split())


class LocationIndex:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._rows: list[tuple[str, str, str, str, float, float, str, str, bool]] | None = None
        self._lock = threading.Lock()

    def rows(self) -> list[tuple[str, str, str, str, float, float, str, str, bool]]:
        with self._lock:
            if self._rows is None:
                try:
                    data = json.loads(self.path.read_text(encoding="utf-8"))
                    self._rows = [
                        (str(r[0]), str(r[1]), str(r[2]), str(r[3]), float(r[4]), float(r[5]), fold(r[1]), fold(r[3]),
                         bool(r[6]) if len(r) > 6 else False)
                        for r in data.get("rows", [])
                    ]
                except (OSError, ValueError, json.JSONDecodeError, IndexError) as exc:
                    LOGGER.warning("Postleitzahlen konnten nicht geladen werden: %s", exc)
                    self._rows = []
            return self._rows

    def search(self, query: str) -> tuple[list[dict[str, Any]], bool]:
        postcode, place = parse_query(query)
        place_folded = fold(place)
        if not postcode and len(place_folded) < 2:
            return [], False
        matches: list[tuple[int, str, tuple]] = []
        for row in self.rows():
            code, name, state, district, lat, lon, name_f, district_f, _secondary = row
            if postcode and not code.startswith(postcode):
                continue
            rank = 0
            if place_folded:
                if name_f == place_folded:
                    rank = 0
                elif name_f.startswith(place_folded):
                    rank = 1
                elif f" {place_folded}" in f" {name_f}":
                    rank = 2
                elif place_folded in name_f:
                    rank = 3
                elif place_folded in district_f:
                    rank = 4
                else:
                    continue
            elif code != postcode:
                rank = 1
            matches.append((rank, code, row))
        # Entries without a verified position (often large-customer postcodes
        # of companies or authorities) only appear when nothing else matches.
        if any(not item[2][8] for item in matches):
            matches = [item for item in matches if not item[2][8] or (item[0] == 0 and place_folded)]
        matches.sort(key=lambda item: (item[0], item[2][1], item[1]))
        results = [self.entry(row) for _rank, _code, row in matches[:MAX_RESULTS]]
        return results, len(matches) > MAX_RESULTS

    @staticmethod
    def entry(row: tuple) -> dict[str, Any]:
        code, name, state, district, lat, lon = row[:6]
        region = ", ".join(part for part in (district if district and district != name else "", state) if part)
        return {
            "postcode": code,
            "name": name,
            "region": region,
            "country": "DE",
            "latitude": lat,
            "longitude": lon,
            "label": f"{code} {name}",
        }


def open_meteo_search(query: str, fetch: Callable[[str, int], bytes], timeout: int = 10) -> list[dict[str, Any]]:
    """Fallback for place names outside the German postcode data."""
    _postcode, place = parse_query(query)
    if len(place) < 2:
        return []
    url = "https://geocoding-api.open-meteo.com/v1/search?" + urllib.parse.urlencode(
        {"name": place, "count": 10, "language": "de", "format": "json"}
    )
    data = json.loads(fetch(url, timeout).decode("utf-8"))
    results = []
    for item in data.get("results") or []:
        try:
            lat, lon = float(item["latitude"]), float(item["longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        postcodes = item.get("postcodes") or []
        country = str(item.get("country") or item.get("country_code") or "")
        region = ", ".join(part for part in (item.get("admin1"), country) if part)
        name = str(item.get("name") or "")
        results.append({
            "postcode": str(postcodes[0]) if postcodes else "",
            "name": name,
            "region": region,
            "country": str(item.get("country_code") or ""),
            "latitude": round(lat, 4),
            "longitude": round(lon, 4),
            "label": f"{postcodes[0]} {name}" if postcodes else name,
        })
    return results
