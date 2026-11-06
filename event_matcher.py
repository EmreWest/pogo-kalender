"""Assign infographics to calendar events.

The matcher works on plain text (Discord message text plus OCR output) and
the event list that the web server already builds for ``/api/events``. It
reuses the existing Pokémon species index of ``app.py`` through the
``species_lookup`` callback, so German and English names are recognised the
same way as everywhere else in the application.

Scoring (defaults, all thresholds configurable):

    date or date range          up to 50
    matching start/end time     up to 10
    Pokémon                     30   (contradicting Pokémon: -20)
    event type                  20   (contradicting type: -10)
    title words                 up to 20
    plausible posting time      10   (posted after the event: -40)

An automatic assignment needs at least ``auto_match_threshold`` points, a
recognised date and a clear lead over the second-best event
(``min_margin``). Otherwise the infographic is offered for manual review
(``manual_review_threshold``) or stays ``pending`` and is re-evaluated when
new events appear.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time as datetime_time, timedelta, timezone, tzinfo
from typing import Any, Callable

SpeciesLookup = Callable[[str], list[dict[str, Any]]]

MONTHS = {
    "jan": 1, "januar": 1, "january": 1, "jän": 1, "jänner": 1,
    "feb": 2, "februar": 2, "february": 2,
    "mär": 3, "märz": 3, "maerz": 3, "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "mai": 5, "may": 5,
    "jun": 6, "juni": 6, "june": 6,
    "jul": 7, "juli": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "okt": 10, "oct": 10, "oktober": 10, "october": 10,
    "nov": 11, "november": 11,
    "dez": 12, "dec": 12, "dezember": 12, "december": 12,
}
MONTH_RE = "(?:" + "|".join(sorted((re.escape(m) for m in MONTHS), key=len, reverse=True)) + r")\.?"
DAY_RE = r"(?:[12][0-9]|3[01]|0?[1-9])"

DAY_MONTH = re.compile(rf"(?<![\d:])(?P<day>{DAY_RE})(?:st|nd|rd|th)?\.?\s*(?P<mon>{MONTH_RE})(?![a-zäöü])")
MONTH_DAY = re.compile(rf"(?<![a-zäöü])(?P<mon>{MONTH_RE})\s*(?P<day>{DAY_RE})(?:st|nd|rd|th)?(?![\d:])")
NUMERIC = re.compile(rf"(?<![\d.:])(?P<day>{DAY_RE})\.(?P<month>1[0-2]|0?[1-9])\.(?P<year>20\d\d|\d\d)?(?![\d])")
DAY_DAY_MONTH = re.compile(
    rf"(?<![\d:])(?P<d1>{DAY_RE})\.?\s*-\s*(?P<d2>{DAY_RE})\.?\s*(?P<mon>{MONTH_RE})(?![a-zäöü])"
)
MONTH_DAY_DAY = re.compile(
    rf"(?<![a-zäöü])(?P<mon>{MONTH_RE})\s*(?P<d1>{DAY_RE})\s*-\s*(?P<d2>{DAY_RE})(?![\d:])"
)
DAY_DAY_NUMERIC = re.compile(
    rf"(?<![\d.:])(?P<d1>{DAY_RE})\.?\s*-\s*(?P<d2>{DAY_RE})\.(?P<month>1[0-2]|0?[1-9])\.(?P<year>20\d\d)?(?![\d])"
)
TIME_24 = re.compile(r"(?<![\d:])(?P<h>[01]?\d|2[0-3]):(?P<m>[0-5]\d)(?![\d])")
TIME_12 = re.compile(r"(?<![\d:])(?P<h>1[0-2]|0?[1-9])(?::(?P<m>[0-5]\d))?\s*(?P<ampm>a\.?m\.?|p\.?m\.?)(?![a-z])")
TIME_UHR = re.compile(r"(?<![\d:])(?P<h>[01]?\d|2[0-3])\s*uhr\b")

# Specific event types first; generic types are only used when no specific
# keyword of the same family was found.
TYPE_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("raid-hour", ("raid hour", "raid-stunde", "raidstunde", "raid stunde")),
    ("raid-day", ("raid day", "raid-tag", "raidtag", "raid tag")),
    ("pokemon-spotlight-hour", ("spotlight", "rampenlicht")),
    ("community-day", ("community day", "community-day", "communityday", "c-day")),
    ("max-mondays", ("max monday", "max mondays", "dyna-montag", "dynamax-montag", "dyna montag")),
    ("research-day", ("research day", "forschungstag")),
    ("limited-research", ("limited research", "befristete forschung")),
    ("pokemon-go-fest", ("go fest", "go-fest")),
    ("pokemon-go-tour", ("go tour", "go-tour")),
    ("raid-battles", ("raid", "schwäche", "weakness", "trainer benötigt", "trainers recommended", "konter", "counters")),
    ("max-battles", ("max battle", "dyna-kampf", "dyna-kämpfe", "dynamax", "gigantamax", "gigadynamax", "dyna-")),
]
FAMILIES = {
    "raid-battles": {"raid-hour", "raid-day", "raid-battles"},
    "max-battles": {"max-mondays", "max-battles"},
}
KNOWN_TYPES = {name for name, _ in TYPE_KEYWORDS} | {"event"}

STOPWORDS = {
    "and", "the", "in", "of", "a", "an", "to", "for", "with", "during", "der", "die", "das", "und", "im", "am",
    "mit", "von", "zum", "zur", "raid", "raids", "battles", "battle", "hour", "day", "event", "events", "pokemon",
    "pokémon", "go", "star", "stufe", "5-star", "edition", "forme", "form", "returns", "debut", "2025", "2026",
    "2027", "2028", "kämpfe", "tag", "stunde", "spotlight", "mega", "super", "shadow", "max", "monday", "mondays",
    "community", "dynamax", "gigantamax", "classic", "normal", "attack", "defense", "speed",
}
WORD = re.compile(r"[a-zäöüß][a-zäöüß'-]{2,}")


@dataclass
class Features:
    ranges: list[tuple[date, date]] = field(default_factory=list)
    dates: set[date] = field(default_factory=set)
    times: set[tuple[int, int]] = field(default_factory=set)
    pokemon_ids: set[int] = field(default_factory=set)
    pokemon_names: list[str] = field(default_factory=list)
    types: set[str] = field(default_factory=set)
    words: set[str] = field(default_factory=set)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dates": sorted(d.isoformat() for d in self.dates),
            "ranges": [[a.isoformat(), b.isoformat()] for a, b in self.ranges],
            "times": sorted(f"{h:02d}:{m:02d}" for h, m in self.times),
            "pokemon": self.pokemon_names,
            "types": sorted(self.types),
        }


def normalize_text(text: str) -> str:
    value = str(text or "").casefold()
    value = re.sub(r"[‐-―−~]", "-", value)
    value = re.sub(r"\s+(?:bis|to|till|until)\s+", " - ", value)
    value = value.replace("’", "'")
    return re.sub(r"[ \t\r\f\v]+", " ", value)


def infer_year(day: int, month: int, reference: date) -> date | None:
    """Pick the year that places day/month closest after ``reference - 60 days``."""
    best: date | None = None
    for year in (reference.year - 1, reference.year, reference.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if candidate >= reference - timedelta(days=60) and (best is None or candidate < best):
            best = candidate
    return best


def _month(value: str) -> int | None:
    return MONTHS.get(value.rstrip(".").casefold())


def extract_dates(text: str, reference: date) -> tuple[list[tuple[date, date]], set[date]]:
    normalized = normalize_text(text)
    ranges: list[tuple[date, date]] = []
    singles: list[tuple[int, int, date]] = []  # (start, end, date) spans for range building

    def add_range(first: date | None, second: date | None) -> None:
        if not first or not second:
            return
        if second < first:
            try:
                second = second.replace(year=second.year + 1)
            except ValueError:
                return
        if 0 <= (second - first).days <= 60 and (first, second) not in ranges:
            ranges.append((first, second))

    for match in DAY_DAY_MONTH.finditer(normalized):
        month = _month(match.group("mon"))
        if month:
            add_range(infer_year(int(match.group("d1")), month, reference), infer_year(int(match.group("d2")), month, reference))
    for match in MONTH_DAY_DAY.finditer(normalized):
        month = _month(match.group("mon"))
        if month:
            add_range(infer_year(int(match.group("d1")), month, reference), infer_year(int(match.group("d2")), month, reference))
    for match in DAY_DAY_NUMERIC.finditer(normalized):
        month = int(match.group("month"))
        add_range(infer_year(int(match.group("d1")), month, reference), infer_year(int(match.group("d2")), month, reference))

    for pattern in (DAY_MONTH, MONTH_DAY):
        for match in pattern.finditer(normalized):
            month = _month(match.group("mon"))
            if not month:
                continue
            value = infer_year(int(match.group("day")), month, reference)
            if value:
                singles.append((match.start(), match.end(), value))
    for match in NUMERIC.finditer(normalized):
        day, month = int(match.group("day")), int(match.group("month"))
        year_text = match.group("year")
        value: date | None
        if year_text:
            year = int(year_text) + (2000 if len(year_text) == 2 else 0)
            try:
                value = date(year, month, day)
            except ValueError:
                value = None
        else:
            value = infer_year(day, month, reference)
        if value:
            singles.append((match.start(), match.end(), value))

    singles.sort()
    for (s1, e1, d1), (s2, _e2, d2) in zip(singles, singles[1:]):
        gap = normalized[e1:s2]
        if re.fullmatch(r"\s*-\s*", gap):
            add_range(d1, d2)

    dates = {value for _s, _e, value in singles}
    for first, second in ranges:
        dates.update({first, second})
    return ranges, dates


def extract_times(text: str) -> set[tuple[int, int]]:
    normalized = normalize_text(text)
    times: set[tuple[int, int]] = set()
    for match in TIME_24.finditer(normalized):
        times.add((int(match.group("h")), int(match.group("m"))))
    for match in TIME_12.finditer(normalized):
        hour = int(match.group("h")) % 12
        if match.group("ampm").startswith("p"):
            hour += 12
        times.add((hour, int(match.group("m") or 0)))
    for match in TIME_UHR.finditer(normalized):
        times.add((int(match.group("h")), 0))
    return times


def extract_types(text: str) -> set[str]:
    normalized = normalize_text(text)
    found: set[str] = set()
    for event_type, keywords in TYPE_KEYWORDS:
        if any(keyword in normalized for keyword in keywords):
            found.add(event_type)
    # A specific raid/max type replaces the generic family member.
    for generic, family in FAMILIES.items():
        if generic in found and (found & family) - {generic}:
            found.discard(generic)
    return found


def extract_words(text: str) -> set[str]:
    return {word.strip("'-") for word in WORD.findall(normalize_text(text)) if len(word.strip("'-")) >= 4}


def extract_features(text: str, posted_at: datetime, local_tz: tzinfo, species_lookup: SpeciesLookup) -> Features:
    reference = posted_at.astimezone(local_tz).date()
    ranges, dates = extract_dates(text, reference)
    features = Features(ranges=ranges, dates=dates, times=extract_times(text), types=extract_types(text), words=extract_words(text))
    for species in species_lookup(text or ""):
        try:
            species_id = int(species["id"])
        except (KeyError, TypeError, ValueError):
            continue
        if species_id not in features.pokemon_ids:
            features.pokemon_ids.add(species_id)
            features.pokemon_names.append(str(species.get("german") or species.get("english") or species_id))
    return features


def parse_event_datetime(value: Any, local_tz: tzinfo) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=local_tz)
    return parsed.astimezone(local_tz)


@dataclass
class EventProfile:
    event_id: str
    title: str
    start: datetime
    end: datetime
    all_day: bool
    event_type: str
    is_manual: bool
    pokemon_ids: set[int]
    title_words: set[str]

    @property
    def start_date(self) -> date:
        return self.start.date()

    @property
    def end_date(self) -> date:
        end = self.end
        if self.all_day or (end.time() == datetime_time(0, 0) and end > self.start):
            end = end - timedelta(seconds=1)
        return end.date()


def build_profile(event: dict[str, Any], local_tz: tzinfo, species_lookup: SpeciesLookup) -> EventProfile | None:
    start = parse_event_datetime(event.get("start"), local_tz)
    end = parse_event_datetime(event.get("end"), local_tz)
    if start is None or end is None:
        return None
    title = str(event.get("title") or "")
    source_title = str(event.get("sourceTitle") or "")
    pokemon_ids: set[int] = set()
    for species in species_lookup(f"{title} {source_title}"):
        try:
            pokemon_ids.add(int(species["id"]))
        except (KeyError, TypeError, ValueError):
            pass
    for image in event.get("pokemonImages") or []:
        if isinstance(image, dict) and isinstance(image.get("pokemonId"), int):
            pokemon_ids.add(int(image["pokemonId"]))
    pokemon_name_words = {
        word for species in species_lookup(f"{title} {source_title}")
        for word in extract_words(f"{species.get('english', '')} {species.get('german', '')}")
    }
    words = {
        word for word in (extract_words(title) | extract_words(source_title)) - STOPWORDS - pokemon_name_words
        if not _is_type_word(word)
    }
    return EventProfile(
        event_id=str(event.get("id") or ""),
        title=title,
        start=start,
        end=end,
        all_day=bool(event.get("allDay")),
        event_type=str(event.get("eventType") or "event").casefold(),
        is_manual=bool(event.get("isManual")),
        pokemon_ids=pokemon_ids,
        title_words=words,
    )


TYPE_WORDS = {
    part
    for _event_type, keywords in TYPE_KEYWORDS
    for keyword in keywords
    for part in re.split(r"[\s-]+", keyword)
    if len(part) >= 4
} | {"crypto-raids", "mega-raids", "stufe-5-raids", "rampenlichtstunde", "dyna-kämpfe", "dyna-montag"}


def _is_type_word(word: str) -> bool:
    """Words that only describe the event type are scored by the type rule."""
    if word in TYPE_WORDS:
        return True
    return any(len(part) >= 5 and part in word for part in TYPE_WORDS)


def _word_found(word: str, text_words: set[str], glossary: dict[str, list[str]]) -> bool:
    variants = {word, *glossary.get(word, [])}
    for variant in variants:
        if variant in text_words:
            return True
        if len(variant) >= 5 and difflib.get_close_matches(variant, text_words, n=1, cutoff=0.84):
            return True
        if len(variant) >= 6 and any(variant in candidate for candidate in text_words if len(candidate) > len(variant)):
            return True
    return False


def score_event(
    features: Features,
    profile: EventProfile,
    posted_at: datetime,
    glossary: dict[str, list[str]],
) -> tuple[int, list[str], dict[str, int]]:
    reasons: list[str] = []
    parts = {"date": 0, "time": 0, "pokemon": 0, "type": 0, "title": 0, "posted": 0}

    # 1. Date
    start_date, end_date = profile.start_date, profile.end_date
    single_day = start_date == end_date
    if (start_date, end_date) in features.ranges:
        parts["date"] = 50
        reasons.append("Datumsbereich stimmt exakt")
    elif single_day and start_date in features.dates:
        parts["date"] = 50
        reasons.append("Datum stimmt")
    elif not single_day and start_date in features.dates and end_date in features.dates:
        parts["date"] = 45
        reasons.append("Start- und Enddatum gefunden")
    elif start_date in features.dates or end_date in features.dates:
        parts["date"] = 35
        reasons.append("Start- oder Enddatum stimmt")
    elif any(start_date <= d <= end_date for d in features.dates):
        parts["date"] = 15
        reasons.append("Datum liegt im Eventzeitraum")

    # 2. Time of day
    if not profile.all_day and features.times:
        event_times = {(profile.start.hour, profile.start.minute), (profile.end.hour, profile.end.minute)}
        hits = event_times & features.times
        if len(hits) == 2 or (hits and event_times and len(event_times) == 1):
            parts["time"] = 10
            reasons.append("Uhrzeiten stimmen")
        elif hits:
            parts["time"] = 6
            reasons.append("Uhrzeit stimmt")

    # 3. Pokémon
    if features.pokemon_ids and profile.pokemon_ids:
        if features.pokemon_ids & profile.pokemon_ids:
            parts["pokemon"] = 30
            reasons.append("Pokémon stimmt")
        else:
            parts["pokemon"] = -20
            reasons.append("andere Pokémon")

    # 4. Event type
    if features.types and not profile.is_manual and profile.event_type in KNOWN_TYPES:
        if profile.event_type in features.types:
            parts["type"] = 20
            reasons.append("Eventtyp stimmt")
        elif any(profile.event_type in family and features.types & family for family in FAMILIES.values()):
            parts["type"] = 8
            reasons.append("verwandter Eventtyp")
        elif profile.event_type != "event" or features.types - {"raid-battles", "max-battles"}:
            parts["type"] = -10
            reasons.append("anderer Eventtyp")

    # 5. Title words
    if profile.title_words:
        found = [word for word in profile.title_words if _word_found(word, features.words, glossary)]
        if found:
            parts["title"] = round(20 * len(found) / len(profile.title_words))
            reasons.append("Titel ähnlich")

    # 6. Posting time
    if posted_at > profile.end + timedelta(days=1):
        parts["posted"] = -40
        reasons.append("nach Eventende veröffentlicht")
    elif profile.start - timedelta(days=30) <= posted_at <= profile.end:
        parts["posted"] = 10

    return sum(parts.values()), reasons, parts


def match_infographic(
    text: str,
    posted_at: datetime,
    events: list[dict[str, Any]],
    settings: dict[str, Any],
    species_lookup: SpeciesLookup,
    local_tz: tzinfo,
) -> dict[str, Any]:
    """Score all events and return the matching decision for one infographic."""
    matching = settings.get("matching") or {}
    auto_threshold = int(matching.get("auto_match_threshold", 80))
    review_threshold = int(matching.get("manual_review_threshold", 50))
    min_margin = int(matching.get("min_margin", 15))
    max_candidates = int(matching.get("max_candidates", 5))
    glossary = matching.get("title_glossary") or {}

    if posted_at.tzinfo is None:
        posted_at = posted_at.replace(tzinfo=timezone.utc)
    features = extract_features(text, posted_at, local_tz, species_lookup)

    scored: list[dict[str, Any]] = []
    for event in events:
        profile = build_profile(event, local_tz, species_lookup)
        if profile is None or not profile.event_id:
            continue
        score, reasons, parts = score_event(features, profile, posted_at, glossary)
        if score <= 0:
            continue
        scored.append({
            "event_id": profile.event_id,
            "title": profile.title,
            "start": profile.start.isoformat(),
            "end": profile.end.isoformat(),
            "is_manual": profile.is_manual,
            "score": score,
            "reasons": reasons,
            "parts": parts,
        })
    scored.sort(key=lambda item: (-item["score"], item["start"]))
    candidates = [c for c in scored if c["score"] >= max(20, review_threshold // 2)][:max_candidates]

    best = scored[0] if scored else None
    second = scored[1] if len(scored) > 1 else None
    margin = best["score"] - second["score"] if best and second else (best["score"] if best else 0)
    result: dict[str, Any] = {
        "status": "pending",
        "event_id": None,
        "event_title": None,
        "score": best["score"] if best else 0,
        "reasons": list(best["reasons"]) if best else [],
        "candidates": candidates,
        "features": features.as_dict(),
    }
    if best is None:
        result["reasons"] = ["kein passendes Event gefunden"]
        return result

    if best["score"] >= auto_threshold and best["parts"]["date"] > 0 and margin >= min_margin:
        result.update({"status": "matched", "event_id": best["event_id"], "event_title": best["title"]})
    elif best["score"] >= review_threshold:
        result["status"] = "manual_review"
        if best["score"] >= auto_threshold and margin < min_margin:
            result["reasons"] = result["reasons"] + [f"mehrere Events ähnlich gut (Abstand {margin})"]
        elif best["score"] >= auto_threshold:
            result["reasons"] = result["reasons"] + ["kein Datum erkannt"]
    return result
