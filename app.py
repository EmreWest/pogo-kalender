#!/usr/bin/env python3
"""Filtered ScrapedDuck event feed exposed as a subscribable iCalendar URL."""

from __future__ import annotations

import csv
import hashlib
import hmac
import html
import io
import json
import logging
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import infographic_store
from infographic_manager import InfographicManager

DEFAULT_CONFIG: dict[str, Any] = {
    "source_url": "https://raw.githubusercontent.com/bigfoott/ScrapedDuck/refs/heads/data/events.json",
    "calendar_name": "Pokémon GO – gefilterte Events",
    "calendar_description": "Gefilterte Pokémon-GO-Termine. Daten: LeekDuck.com via ScrapedDuck.",
    "timezone": "Europe/Berlin",
    "calendar_path": "/pokemon-go.ics",
    "cache_seconds": 1800,
    "request_timeout_seconds": 20,
    "translations": {
        "enabled": True,
        "translate_pokemon_names": True,
        "pokemon_names_url": "https://raw.githubusercontent.com/PokeAPI/pokeapi/master/data/v2/csv/pokemon_species_names.csv",
        "pokemon_names_cache_file": "/data/pokemon_names-de.json",
        "bundled_pokemon_names_file": "/app/pokemon_names-de.json",
        "pokemon_names_cache_seconds": 604800,
        "manual_pokemon_names": {},
        "manual_phrases": {},
    },
    "manual_events": {
        "enabled": True,
        "file": "/data/manual-events.json",
        "admin_password": "CHANGE-ME-NOW-2026!",
        "session_hours": 12,
        "secure_cookie": True,
        "max_events": 500,
        "login_attempts": 5,
        "login_window_seconds": 900,
    },
    "pokemon_images": {
        "enabled": True,
        "event_types": ["raid-battles", "raid-hour", "raid-day", "max-battles", "max-mondays"],
        "cache_dir": "/data/pokemon-images",
        "cache_seconds": 2592000,
        "max_image_bytes": 5242880,
        "max_per_event": 3,
        "use_scrapedduck_images": True,
    },
    "infographics": infographic_store.DEFAULT_SETTINGS,
    "filters": {
        "include_types": [],
        "exclude_types": ["go-battle-league", "season", "update"],
        # Types with local in-person editions (Sendai, Los Angeles, …): only the
        # global edition is relevant for players in Augsburg.
        "global_only_types": ["wild-area", "pokemon-go-tour", "pokemon-go-fest"],
        "include_keywords": [],
        "exclude_keywords": [],
        "require_spawns": None,
        "require_field_research": None,
        "past_days": 3,
        "future_days": 365,
    },
}

CONFIG_FILE = Path(os.environ.get("CONFIG_FILE", "/config/config.json"))
PORT = int(os.environ.get("PORT", "8080"))
LANDING_FILE = Path(os.environ.get("LANDING_FILE", "/app/landing.html"))
STATUS_FILE = Path(os.environ.get("STATUS_FILE", "/app/status.html"))
ADMIN_FILE = Path(os.environ.get("ADMIN_FILE", "/app/admin.html"))
POKEMON_SPECIES_FILE = Path(os.environ.get("POKEMON_SPECIES_FILE", "/app/pokemon_species-de.json"))
STATIC_ASSETS = {
    "/favicon.svg": (Path("/app/favicon.svg"), "image/svg+xml; charset=utf-8"),
    "/favicon.ico": (Path("/app/favicon.ico"), "image/x-icon"),
    "/favicon-32x32.png": (Path("/app/favicon-32x32.png"), "image/png"),
    "/apple-touch-icon.png": (Path("/app/apple-touch-icon.png"), "image/png"),
    "/icon-192.png": (Path("/app/icon-192.png"), "image/png"),
    "/icon-512.png": (Path("/app/icon-512.png"), "image/png"),
    "/site.webmanifest": (Path("/app/site.webmanifest"), "application/manifest+json; charset=utf-8"),
}
SERVICE_VERSION = "1.7.0"
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s",
)
LOGGER = logging.getLogger("scrapedduck-ical")

CACHE_LOCK = threading.Lock()
CACHE: dict[str, Any] = {
    "created_monotonic": 0.0,
    "body": None,
    "etag": None,
    "source_count": 0,
    "filtered_count": 0,
    "stale": False,
    "last_error": None,
    "web_events": [],
    "last_success_at": None,
    "last_attempt_at": None,
    "source_events": [],
    "manual_count": 0,
    "manual_updated_at": None,
}

POKEMON_NAMES_LOCK = threading.Lock()
POKEMON_NAMES_CACHE: dict[str, Any] = {
    "mapping": None,
    "pattern": None,
    "loaded_monotonic": 0.0,
    "source": None,
}

POKEMON_SPECIES_LOCK = threading.Lock()
POKEMON_SPECIES_CACHE: dict[str, Any] = {
    "mapping": None,
    "pattern": None,
}

IMAGE_SOURCE_LOCK = threading.Lock()
IMAGE_CACHE_LOCK = threading.Lock()
IMAGE_SOURCES: dict[str, dict[str, str]] = {}
ALLOWED_IMAGE_HOSTS = {"cdn.leekduck.com", "raw.githubusercontent.com"}


MANUAL_EVENTS_LOCK = threading.Lock()
SESSIONS_LOCK = threading.Lock()
ADMIN_SESSIONS: dict[str, dict[str, Any]] = {}
LOGIN_ATTEMPTS_LOCK = threading.Lock()
LOGIN_ATTEMPTS: dict[str, list[float]] = {}
ADMIN_COOKIE_NAME = "pogo_admin_session"
DEFAULT_ADMIN_PASSWORD = "CHANGE-ME-NOW-2026!"
INFOGRAPHICS = InfographicManager()

DEFAULT_PHRASE_TRANSLATIONS: dict[str, str] = {
    "Pokémon GO Wild Area": "Pokémon GO Naturzone",
    "GO Wild Area": "GO Naturzone",
    "Pokémon Spotlight Hour": "Pokémon-Rampenlichtstunde",
    " Spotlight Hour": "-Rampenlichtstunde",
    "Spotlight Hour": "Rampenlichtstunde",
    "in 5-star Raid Battles": "in Stufe-5-Raids",
    "in Mega Raids": "in Mega-Raids",
    "in Shadow Raids": "in Crypto-Raids",
    "Raid Battles": "Raid-Kämpfe",
    " Raid Hour": "-Raid-Stunde",
    "Raid Hour": "Raid-Stunde",
    "Max Battle Day": "Dyna-Kampftag",
    "Max Battles": "Dyna-Kämpfe",
    "during Max Monday": "am Dyna-Montag",
    "Max Mondays": "Dyna-Montage",
    "Max Monday": "Dyna-Montag",
    "Gigantamax": "Gigadynamax",
    "Shadow ": "Crypto-",
    "Alolan ": "Alola-",
    "Galarian ": "Galar-",
    "Hisuian ": "Hisui-",
    "Paldean ": "Paldea-",
    "Community Day Classic": "Community Day Classic",
    "Community Day": "Community Day",
    "Research Day": "Forschungstag",
    "Hatch Day": "Schlüpftag",
    "GO Battle League": "GO-Kampfliga",
    "Ultra Unlock: 10th Anniversary Edition": "Hyperbonus: Edition zum 10. Jubiläum",
    "Choose Your Path: Fairy Trail": "Wähle deinen Weg: Feenpfad",
    "Choose Your Path": "Wähle deinen Weg",
    "Fire and Ice Hatch Day": "Schlüpftag: Feuer und Eis",
    "Summer Marathon: Arctic Embers": "Sommermarathon: Arktische Glut",
    "Summer Marathon": "Sommermarathon",
    "Hatch Day": "Schlüpftag",
    "Raid Day": "Raid-Tag",
    "Increased Spawns": "Mehr wilde Pokémon",
    "Transfer Candy": "Bonbons beim Verschicken",
    "Catch Candy": "Fang-Bonbons",
    "Catch XP": "Fang-EP",
    "Catch Stardust": "Fang-Sternenstaub",
    "Evolution XP": "Entwicklungs-EP",
    ", and ": " und ",
    " and ": " und ",
}

EVENT_TYPE_LABELS: dict[str, str] = {
    "community-day": "Community Day",
    "raid-day": "Raid-Tag",
    "raid-hour": "Raid-Stunde",
    "raid-battles": "Raid-Kämpfe",
    "pokemon-spotlight-hour": "Pokémon-Rampenlichtstunde",
    "research-day": "Forschungstag",
    "limited-research": "Befristete Forschung",
    "pokemon-go-fest": "Pokémon GO Fest",
    "pokemon-go-tour": "Pokémon GO Tour",
    "wild-area": "GO Naturzone",
    "timed-research": "Zeitlich begrenzte Forschung",
    "research": "Forschung",
    "go-pass": "GO-Pass",
    "season": "Saison",
    "max-battles": "Dyna-Kämpfe",
    "max-mondays": "Dyna-Montag",
    "go-battle-league": "GO-Kampfliga",
    "event": "Event",
    "manual-event": "Manuelles Event",
    "manual-meetup": "Treffen",
    "manual-raid": "Raid-Treffen",
    "manual-reminder": "Erinnerung",
}


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config() -> dict[str, Any]:
    config = DEFAULT_CONFIG
    if CONFIG_FILE.exists():
        try:
            with CONFIG_FILE.open("r", encoding="utf-8") as handle:
                user_config = json.load(handle)
            if not isinstance(user_config, dict):
                raise ValueError("Die Konfiguration muss ein JSON-Objekt sein.")
            config = deep_merge(DEFAULT_CONFIG, user_config)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise RuntimeError(f"Konfiguration kann nicht geladen werden: {exc}") from exc

    path = str(config.get("calendar_path", "/pokemon-go.ics")).strip()
    if not path.startswith("/"):
        path = "/" + path
    config["calendar_path"] = path

    try:
        ZoneInfo(str(config["timezone"]))
    except ZoneInfoNotFoundError as exc:
        raise RuntimeError(f"Unbekannte Zeitzone: {config['timezone']}") from exc

    return config



def manual_config(config: dict[str, Any]) -> dict[str, Any]:
    value = config.get("manual_events") or {}
    return value if isinstance(value, dict) else {}


def manual_events_path(config: dict[str, Any]) -> Path:
    return Path(str(manual_config(config).get("file") or "/data/manual-events.json"))


def ensure_manual_events_file(config: dict[str, Any]) -> Path:
    path = manual_events_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text('{\n  "events": []\n}\n', encoding="utf-8")
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        temporary.replace(path)
    return path


def load_manual_events(config: dict[str, Any]) -> list[dict[str, Any]]:
    if not bool(manual_config(config).get("enabled", True)):
        return []
    with MANUAL_EVENTS_LOCK:
        path = ensure_manual_events_file(config)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Manuelle Events können nicht geladen werden: {exc}") from exc
        raw_events = payload.get("events") if isinstance(payload, dict) else payload
        if not isinstance(raw_events, list):
            raise RuntimeError("manual-events.json muss ein Objekt mit einer Event-Liste enthalten.")
        return [dict(item) for item in raw_events if isinstance(item, dict)]


def save_manual_events(config: dict[str, Any], events: list[dict[str, Any]]) -> None:
    max_events = max(1, min(int(manual_config(config).get("max_events", 500)), 5000))
    if len(events) > max_events:
        raise ValueError(f"Es sind höchstens {max_events} manuelle Events erlaubt.")
    with MANUAL_EVENTS_LOCK:
        path = ensure_manual_events_file(config)
        payload = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "events": sorted(events, key=lambda item: str(item.get("start") or "")),
        }
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        temporary.replace(path)
        CACHE["manual_updated_at"] = payload["updated_at"]


def manual_file_updated_at(config: dict[str, Any]) -> str | None:
    path = manual_events_path(config)
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
    except OSError:
        return None


def sanitize_text(value: Any, maximum: int, field_name: str, required: bool = False) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise ValueError(f"{field_name} darf nicht leer sein.")
    if len(text) > maximum:
        raise ValueError(f"{field_name} darf höchstens {maximum} Zeichen enthalten.")
    return text


def validate_http_url(value: Any) -> str:
    text = sanitize_text(value, 1000, "URL")
    if not text:
        return ""
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Die URL muss mit http:// oder https:// beginnen.")
    return text


def parse_local_form_datetime(value: Any, local_tz: ZoneInfo, field_name: str) -> datetime:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field_name} enthält kein gültiges Datum mit Uhrzeit.") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=local_tz)
    return parsed.astimezone(local_tz)


def validate_manual_event_payload(
    payload: dict[str, Any],
    config: dict[str, Any],
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Ungültige Eventdaten.")
    local_tz = ZoneInfo(str(config["timezone"]))
    all_day = bool(payload.get("all_day", False))
    title = sanitize_text(payload.get("title"), 200, "Titel", required=True)
    description = sanitize_text(payload.get("description"), 5000, "Beschreibung")
    location = sanitize_text(payload.get("location"), 300, "Ort")
    url = validate_http_url(payload.get("url"))
    event_type = sanitize_text(payload.get("type") or "manual-event", 64, "Eventtyp").casefold()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", event_type):
        raise ValueError("Der Eventtyp darf nur Kleinbuchstaben, Zahlen und Bindestriche enthalten.")

    raw_pokemon = payload.get("pokemon")
    if isinstance(raw_pokemon, str):
        pokemon = [part.strip() for part in raw_pokemon.split(",") if part.strip()]
    elif isinstance(raw_pokemon, list):
        pokemon = [sanitize_text(item, 80, "Pokémon-Name") for item in raw_pokemon if str(item or "").strip()]
    else:
        pokemon = []
    pokemon = list(dict.fromkeys(pokemon))[:8]

    if all_day:
        try:
            start_date = date.fromisoformat(str(payload.get("start") or ""))
            inclusive_end = date.fromisoformat(str(payload.get("end") or payload.get("start") or ""))
        except ValueError as exc:
            raise ValueError("Start- und Enddatum müssen gültige Datumswerte sein.") from exc
        if inclusive_end < start_date:
            raise ValueError("Das Enddatum darf nicht vor dem Startdatum liegen.")
        if (inclusive_end - start_date).days > 366:
            raise ValueError("Ein manuelles Event darf höchstens 367 Tage dauern.")
        stored_start = start_date.isoformat()
        stored_end = (inclusive_end + timedelta(days=1)).isoformat()
    else:
        start_dt = parse_local_form_datetime(payload.get("start"), local_tz, "Start")
        end_dt = parse_local_form_datetime(payload.get("end"), local_tz, "Ende")
        if end_dt <= start_dt:
            raise ValueError("Das Ende muss nach dem Start liegen.")
        if end_dt - start_dt > timedelta(days=367):
            raise ValueError("Ein manuelles Event darf höchstens 367 Tage dauern.")
        stored_start = start_dt.isoformat(timespec="minutes")
        stored_end = end_dt.isoformat(timespec="minutes")

    now_iso = datetime.now(timezone.utc).isoformat()
    existing = existing or {}
    event_id = str(existing.get("id") or f"manual-{secrets.token_hex(12)}")
    return {
        "id": event_id,
        "title": title,
        "description": description,
        "start": stored_start,
        "end": stored_end,
        "all_day": all_day,
        "type": event_type,
        "location": location,
        "url": url,
        "pokemon": pokemon,
        "created_at": str(existing.get("created_at") or now_iso),
        "updated_at": now_iso,
    }


def manual_event_datetimes(event: dict[str, Any], local_tz: ZoneInfo) -> tuple[datetime, datetime, bool]:
    all_day = bool(event.get("all_day"))
    if all_day:
        try:
            start_date = date.fromisoformat(str(event.get("start") or ""))
            end_date = date.fromisoformat(str(event.get("end") or ""))
        except ValueError as exc:
            raise ValueError(f"Manuelles Event {event.get('id')}: ungültiges Datum") from exc
        start = datetime.combine(start_date, datetime_time.min, tzinfo=local_tz)
        end = datetime.combine(end_date, datetime_time.min, tzinfo=local_tz)
    else:
        start = parse_source_datetime(event.get("start"), local_tz)
        end = parse_source_datetime(event.get("end"), local_tz)
        if start is None or end is None:
            raise ValueError(f"Manuelles Event {event.get('id')}: ungültiger Zeitstempel")
    if end <= start:
        raise ValueError(f"Manuelles Event {event.get('id')}: Ende liegt nicht nach dem Start")
    return start, end, all_day


def manual_event_in_window(event: dict[str, Any], config: dict[str, Any], now_local: datetime) -> bool:
    try:
        start, end, _ = manual_event_datetimes(event, ZoneInfo(str(config["timezone"])))
    except ValueError as exc:
        LOGGER.warning("%s", exc)
        return False
    filters = config.get("filters") or {}
    past_days = max(int(filters.get("past_days", 3)), 0)
    future_days = max(int(filters.get("future_days", 365)), 0)
    return end >= now_local - timedelta(days=past_days) and start <= now_local + timedelta(days=future_days)


def manual_event_for_admin(event: dict[str, Any]) -> dict[str, Any]:
    result = dict(event)
    if bool(event.get("all_day")):
        try:
            result["end_inclusive"] = (date.fromisoformat(str(event.get("end"))) - timedelta(days=1)).isoformat()
        except ValueError:
            result["end_inclusive"] = str(event.get("end") or "")
    return result


def invalidate_calendar_cache() -> None:
    with CACHE_LOCK:
        # -inf instead of 0.0: time.monotonic() can be smaller than cache_seconds
        # shortly after the host boots, which would keep the old cache alive.
        CACHE["created_monotonic"] = float("-inf")


def admin_password(config: dict[str, Any]) -> str:
    # ADMIN_PASSWORD (environment / .env) takes precedence, so the password
    # does not have to live in config.json.
    from_env = os.environ.get("ADMIN_PASSWORD", "").strip()
    if from_env:
        return from_env
    return str(manual_config(config).get("admin_password") or DEFAULT_ADMIN_PASSWORD)


def prune_sessions() -> None:
    now = time.time()
    with SESSIONS_LOCK:
        expired = [token for token, session in ADMIN_SESSIONS.items() if float(session.get("expires", 0)) <= now]
        for token in expired:
            ADMIN_SESSIONS.pop(token, None)


def session_from_headers(headers: Any) -> tuple[str | None, dict[str, Any] | None]:
    prune_sessions()
    cookie_header = headers.get("Cookie")
    if not cookie_header:
        return None, None
    cookie = SimpleCookie()
    try:
        cookie.load(cookie_header)
    except Exception:
        return None, None
    morsel = cookie.get(ADMIN_COOKIE_NAME)
    if not morsel:
        return None, None
    token = morsel.value
    with SESSIONS_LOCK:
        session = ADMIN_SESSIONS.get(token)
        return token, dict(session) if session else None


def create_admin_session(config: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    hours = max(1, min(int(manual_config(config).get("session_hours", 12)), 168))
    token = secrets.token_urlsafe(36)
    session = {
        "expires": time.time() + hours * 3600,
        "csrf": secrets.token_urlsafe(28),
    }
    with SESSIONS_LOCK:
        ADMIN_SESSIONS[token] = session
    return token, session


def client_ip(headers: Any, fallback: str) -> str:
    for header in ("CF-Connecting-IP", "X-Forwarded-For", "X-Real-IP"):
        value = str(headers.get(header) or "").strip()
        if value:
            return value.split(",", 1)[0].strip()[:128]
    return fallback


def login_allowed(ip: str, config: dict[str, Any]) -> tuple[bool, int]:
    settings = manual_config(config)
    limit = max(1, min(int(settings.get("login_attempts", 5)), 50))
    window = max(60, min(int(settings.get("login_window_seconds", 900)), 86400))
    cutoff = time.time() - window
    with LOGIN_ATTEMPTS_LOCK:
        attempts = [stamp for stamp in LOGIN_ATTEMPTS.get(ip, []) if stamp >= cutoff]
        LOGIN_ATTEMPTS[ip] = attempts
    if len(attempts) >= limit:
        retry = max(1, int(window - (time.time() - attempts[0])))
        return False, retry
    return True, 0


def record_login_failure(ip: str) -> None:
    with LOGIN_ATTEMPTS_LOCK:
        LOGIN_ATTEMPTS.setdefault(ip, []).append(time.time())


def clear_login_failures(ip: str) -> None:
    with LOGIN_ATTEMPTS_LOCK:
        LOGIN_ATTEMPTS.pop(ip, None)

def fetch_events(config: dict[str, Any]) -> list[dict[str, Any]]:
    payload = fetch_bytes(
        str(config["source_url"]),
        int(config.get("request_timeout_seconds", 20)),
        "scrapedduck-ical/1.3 (+self-hosted calendar feed)",
    )

    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"ScrapedDuck lieferte ungültiges JSON: {exc}") from exc

    if not isinstance(data, list):
        raise RuntimeError("ScrapedDuck lieferte keine Event-Liste.")

    return [item for item in data if isinstance(item, dict)]


def fetch_bytes(url: str, timeout: int, user_agent: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": user_agent, "Accept": "*/*"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"Abruf fehlgeschlagen ({url}): {exc}") from exc


def build_pokemon_mapping(csv_payload: bytes) -> dict[str, str]:
    try:
        text = csv_payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"Pokémon-Namensliste ist nicht UTF-8: {exc}") from exc

    by_species: dict[str, dict[str, str]] = {}
    reader = csv.DictReader(io.StringIO(text))
    required = {"pokemon_species_id", "local_language_id", "name"}
    if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
        raise RuntimeError("Pokémon-Namensliste hat ein unerwartetes CSV-Format.")

    for row in reader:
        species_id = str(row.get("pokemon_species_id") or "").strip()
        language_id = str(row.get("local_language_id") or "").strip()
        name = str(row.get("name") or "").strip()
        if not species_id or not name or language_id not in {"6", "9"}:
            continue
        by_species.setdefault(species_id, {})[language_id] = name

    mapping: dict[str, str] = {}
    for names in by_species.values():
        english = names.get("9")
        german = names.get("6")
        if not english or not german:
            continue
        mapping[english] = german
        # ScrapedDuck and other sources sometimes use straight/curly apostrophes differently.
        mapping[english.replace("’", "'")] = german
        mapping[english.replace("'", "’")] = german
    return mapping


def save_json_cache(path: Path, mapping: dict[str, str]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(mapping, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        temporary.replace(path)
    except OSError as exc:
        LOGGER.warning("Pokémon-Namenscache konnte nicht geschrieben werden: %s", exc)


def load_json_cache(path: Path) -> dict[str, str] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return {str(key): str(value) for key, value in data.items() if str(key) and str(value)}


def compile_pokemon_pattern(mapping: dict[str, str]) -> re.Pattern[str] | None:
    if not mapping:
        return None
    alternatives = sorted((re.escape(name) for name in mapping), key=len, reverse=True)
    # Avoid replacing a Pokémon name when it is embedded inside another word.
    return re.compile(r"(?<![\w])(?:" + "|".join(alternatives) + r")(?![\w])", re.IGNORECASE)


def get_pokemon_translator(config: dict[str, Any]) -> tuple[dict[str, str], re.Pattern[str] | None]:
    translations = config.get("translations") or {}
    if not isinstance(translations, dict) or not translations.get("enabled", True):
        return {}, None
    if not translations.get("translate_pokemon_names", True):
        return {}, None

    cache_seconds = max(int(translations.get("pokemon_names_cache_seconds", 604800)), 3600)
    now_monotonic = time.monotonic()

    with POKEMON_NAMES_LOCK:
        cached_mapping = POKEMON_NAMES_CACHE.get("mapping")
        cached_age = now_monotonic - float(POKEMON_NAMES_CACHE.get("loaded_monotonic", 0.0))
        # A valid mapping may stay in memory for the configured duration. An empty
        # mapping is retried after five minutes instead of being cached for a week.
        memory_ttl = cache_seconds if cached_mapping else 300
        if cached_mapping is not None and cached_age < memory_ttl:
            return cached_mapping, POKEMON_NAMES_CACHE.get("pattern")

        cache_path = Path(str(translations.get("pokemon_names_cache_file", "/data/pokemon_names-de.json")))
        bundled_path = Path(str(translations.get("bundled_pokemon_names_file", "/app/pokemon_names-de.json")))
        mapping: dict[str, str] | None = None
        mapping_source = "none"
        cache_is_fresh = False
        try:
            cache_is_fresh = cache_path.exists() and time.time() - cache_path.stat().st_mtime < cache_seconds
        except OSError:
            pass

        if cache_is_fresh:
            mapping = load_json_cache(cache_path)
            if mapping:
                mapping_source = "volume-cache"

        if not mapping:
            url = str(translations.get("pokemon_names_url") or "").strip()
            if url:
                try:
                    payload = fetch_bytes(
                        url,
                        int(config.get("request_timeout_seconds", 20)),
                        "scrapedduck-ical/1.2 (+Pokemon name translation)",
                    )
                    downloaded_mapping = build_pokemon_mapping(payload)
                    if downloaded_mapping:
                        mapping = downloaded_mapping
                        mapping_source = "download"
                        save_json_cache(cache_path, mapping)
                except Exception as exc:
                    LOGGER.warning("Pokémon-Namensliste konnte nicht aktualisiert werden: %s", exc)

        # A stale volume cache is still better than no translation at all.
        if not mapping:
            mapping = load_json_cache(cache_path)
            if mapping:
                mapping_source = "stale-volume-cache"

        # The image contains a complete fallback list. This makes Pokémon name
        # translation independent of GitHub/PokéAPI availability at runtime.
        if not mapping:
            mapping = load_json_cache(bundled_path)
            if mapping:
                mapping_source = "bundled-fallback"

        if mapping is None:
            mapping = {}

        manual = translations.get("manual_pokemon_names") or {}
        if isinstance(manual, dict):
            for english, german in manual.items():
                if str(english).strip() and str(german).strip():
                    mapping[str(english).strip()] = str(german).strip()

        folded_mapping = {name.casefold(): german for name, german in mapping.items()}
        pattern = compile_pokemon_pattern(mapping)
        POKEMON_NAMES_CACHE.update(
            {
                "mapping": folded_mapping,
                "pattern": pattern,
                "loaded_monotonic": now_monotonic,
                "source": mapping_source,
            }
        )
        LOGGER.info(
            "Pokémon-Übersetzungen bereit: %d Namen (Quelle: %s)",
            len(folded_mapping),
            mapping_source,
        )
        return folded_mapping, pattern


def replace_pokemon_names(text: str, mapping: dict[str, str], pattern: re.Pattern[str] | None) -> str:
    if not text or not mapping or pattern is None:
        return text
    return pattern.sub(lambda match: mapping.get(match.group(0).casefold(), match.group(0)), text)


def replace_phrase_case_insensitive(text: str, source: str, target: str) -> str:
    if not source:
        return text
    return re.sub(re.escape(source), lambda _match: target, text, flags=re.IGNORECASE)


def translate_text(
    value: Any,
    config: dict[str, Any],
    pokemon_mapping: dict[str, str],
    pokemon_pattern: re.Pattern[str] | None,
) -> str:
    text = str(value or "")
    translations = config.get("translations") or {}
    if not isinstance(translations, dict) or not translations.get("enabled", True):
        return text

    text = replace_pokemon_names(text, pokemon_mapping, pokemon_pattern)

    phrases = dict(DEFAULT_PHRASE_TRANSLATIONS)
    manual_phrases = translations.get("manual_phrases") or {}
    if isinstance(manual_phrases, dict):
        phrases.update({str(key): str(value) for key, value in manual_phrases.items()})

    for source in sorted(phrases, key=len, reverse=True):
        text = replace_phrase_case_insensitive(text, source, phrases[source])
    return text



def load_pokemon_species_index() -> tuple[dict[str, dict[str, Any]], re.Pattern[str] | None]:
    with POKEMON_SPECIES_LOCK:
        cached = POKEMON_SPECIES_CACHE.get("mapping")
        if isinstance(cached, dict):
            return cached, POKEMON_SPECIES_CACHE.get("pattern")

        try:
            raw = json.loads(POKEMON_SPECIES_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            LOGGER.warning("Pokémon-Artenindex konnte nicht geladen werden: %s", exc)
            POKEMON_SPECIES_CACHE.update({"mapping": {}, "pattern": None})
            return {}, None

        # The lookup index intentionally contains both English and German
        # aliases. ScrapedDuck generally uses English names, while manually
        # created events are commonly entered with German names.
        mapping: dict[str, dict[str, Any]] = {}
        aliases: set[str] = set()
        species_ids: set[int] = set()
        if isinstance(raw, dict):
            for english, details in raw.items():
                if not isinstance(details, dict):
                    continue
                try:
                    species_id = int(details.get("id"))
                except (TypeError, ValueError):
                    continue
                german = str(details.get("de") or english).strip()
                english_name = str(english).strip()
                if not english_name or species_id <= 0:
                    continue

                species = {
                    "id": species_id,
                    "english": english_name,
                    "german": german,
                }
                species_ids.add(species_id)

                for alias in (english_name, german):
                    clean_alias = str(alias or "").strip()
                    if not clean_alias:
                        continue
                    alias_key = clean_alias.casefold()
                    existing = mapping.get(alias_key)
                    # In the unlikely event of an alias collision, retain the
                    # first deterministic match instead of switching species.
                    if existing is None or int(existing.get("id", 0)) == species_id:
                        mapping[alias_key] = species
                        aliases.add(clean_alias)

        pattern = compile_pokemon_pattern({name: name for name in aliases})
        POKEMON_SPECIES_CACHE.update({"mapping": mapping, "pattern": pattern})
        LOGGER.info(
            "Pokémon-Bildindex bereit: %d Arten / %d deutsche und englische Aliase",
            len(species_ids),
            len(mapping),
        )
        return mapping, pattern


def pokemon_species_matches(text: str) -> list[dict[str, Any]]:
    mapping, pattern = load_pokemon_species_index()
    if not text or not mapping or pattern is None:
        return []
    result: list[dict[str, Any]] = []
    seen: set[int] = set()
    for match in pattern.finditer(text):
        details = mapping.get(match.group(0).casefold())
        if not details:
            continue
        species_id = int(details["id"])
        if species_id in seen:
            continue
        seen.add(species_id)
        result.append(details)
    return result


def allowed_image_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    return parsed.scheme == "https" and (parsed.hostname or "").casefold() in ALLOWED_IMAGE_HOSTS


def register_image_source(url: str, name: str) -> str | None:
    clean_url = str(url or "").strip()
    if not allowed_image_url(clean_url):
        return None
    key = hashlib.sha256(clean_url.encode("utf-8")).hexdigest()[:40]
    with IMAGE_SOURCE_LOCK:
        IMAGE_SOURCES[key] = {"url": clean_url, "name": str(name or "Pokémon")}
    return f"/pokemon-image/{key}"


def official_artwork_url(species_id: int) -> str:
    return (
        "https://raw.githubusercontent.com/PokeAPI/sprites/master/"
        f"sprites/pokemon/other/official-artwork/{species_id}.png"
    )


def event_pokemon_images(
    event: dict[str, Any],
    config: dict[str, Any],
    pokemon_mapping: dict[str, str],
    pokemon_pattern: re.Pattern[str] | None,
) -> list[dict[str, Any]]:
    image_config = config.get("pokemon_images") or {}
    if not isinstance(image_config, dict) or not image_config.get("enabled", True):
        return []

    event_type = str(event.get("eventType") or event.get("type") or "").strip().casefold()
    explicit_pokemon = event.get("pokemon")
    enabled_types = set(normalized_string_list(image_config.get("event_types")))
    if enabled_types and event_type not in enabled_types and not explicit_pokemon:
        return []

    max_per_event = max(1, min(int(image_config.get("max_per_event", 3)), 8))
    species_mapping, _ = load_pokemon_species_index()
    candidates: list[dict[str, Any]] = []
    seen_species: set[int] = set()
    seen_names: set[str] = set()

    def add_candidate(source_name: Any, source_image: Any = None) -> None:
        english_name = str(source_name or "").strip()
        if not english_name:
            return
        matches = pokemon_species_matches(english_name)
        species = matches[0] if matches else None
        species_id = int(species["id"]) if species else None
        dedupe_key = str(species_id) if species_id else english_name.casefold()
        if species_id is not None and species_id in seen_species:
            return
        if dedupe_key in seen_names:
            return

        image_url = str(source_image or "").strip()
        if not image_config.get("use_scrapedduck_images", True) or not allowed_image_url(image_url):
            image_url = official_artwork_url(species_id) if species_id is not None else ""
        if not image_url and species_id is not None:
            image_url = official_artwork_url(species_id)
        local_url = register_image_source(image_url, english_name)
        if not local_url:
            return

        translated_name = translate_text(english_name, config, pokemon_mapping, pokemon_pattern).strip()
        if species and (not translated_name or translated_name.casefold() == english_name.casefold()):
            translated_name = re.sub(
                re.escape(str(species["english"])),
                str(species["german"]),
                english_name,
                flags=re.IGNORECASE,
            )
        if species_id is not None:
            seen_species.add(species_id)
        seen_names.add(dedupe_key)
        candidates.append(
            {
                "name": translated_name or english_name,
                "sourceName": english_name,
                "pokemonId": species_id,
                "url": local_url,
            }
        )

    if isinstance(explicit_pokemon, list):
        for pokemon_name in explicit_pokemon:
            add_candidate(pokemon_name)

    extra = event.get("extraData")
    if isinstance(extra, dict):
        raids = extra.get("raidbattles")
        if isinstance(raids, dict):
            bosses = raids.get("bosses")
            if isinstance(bosses, list):
                for boss in bosses:
                    if isinstance(boss, dict):
                        add_candidate(boss.get("name"), boss.get("image"))

        # Kept generic for future ScrapedDuck Max Battle data additions.
        max_data = extra.get("maxbattles")
        if isinstance(max_data, dict):
            for field in ("bosses", "pokemon", "list"):
                values = max_data.get(field)
                if isinstance(values, list):
                    for item in values:
                        if isinstance(item, dict):
                            add_candidate(item.get("name"), item.get("image"))

    # Raid Hours and most Max events currently encode the Pokémon only in the title.
    for species in pokemon_species_matches(str(event.get("name") or "")):
        add_candidate(species["english"], official_artwork_url(int(species["id"])))
        if len(candidates) >= max_per_event:
            break

    return candidates[:max_per_event]


def detect_image_content_type(data: bytes, header_value: str | None = None) -> str | None:
    header = str(header_value or "").split(";", 1)[0].strip().casefold()
    if header in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
        return header
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    return None


def fetch_remote_image(url: str, timeout: int, max_bytes: int) -> tuple[bytes, str]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "scrapedduck-ical/1.5 (+Pokemon image cache)",
            "Accept": "image/avif,image/webp,image/png,image/jpeg,image/*;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > max_bytes:
                raise RuntimeError("Bild ist größer als das konfigurierte Limit.")
            payload = response.read(max_bytes + 1)
            if len(payload) > max_bytes:
                raise RuntimeError("Bild ist größer als das konfigurierte Limit.")
            content_type = detect_image_content_type(payload, response.headers.get("Content-Type"))
            if not content_type:
                raise RuntimeError("Remote-Datei ist kein unterstütztes Bild.")
            return payload, content_type
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise RuntimeError(f"Pokémon-Bild konnte nicht geladen werden: {exc}") from exc


def load_pokemon_image(key: str, config: dict[str, Any]) -> tuple[bytes, str, bool] | None:
    if not re.fullmatch(r"[0-9a-f]{40}", key):
        return None
    image_config = config.get("pokemon_images") or {}
    if not isinstance(image_config, dict) or not image_config.get("enabled", True):
        return None

    cache_dir = Path(str(image_config.get("cache_dir") or "/data/pokemon-images"))
    data_path = cache_dir / f"{key}.image"
    meta_path = cache_dir / f"{key}.json"
    max_age = max(int(image_config.get("cache_seconds", 2592000)), 3600)
    max_bytes = max(262144, min(int(image_config.get("max_image_bytes", 5242880)), 20971520))
    timeout = max(5, min(int(config.get("request_timeout_seconds", 20)), 60))

    with IMAGE_CACHE_LOCK:
        metadata: dict[str, Any] = {}
        try:
            metadata_raw = json.loads(meta_path.read_text(encoding="utf-8"))
            if isinstance(metadata_raw, dict):
                metadata = metadata_raw
        except (OSError, json.JSONDecodeError):
            pass

        cached_payload: bytes | None = None
        cached_type = str(metadata.get("content_type") or "")
        try:
            cached_payload = data_path.read_bytes()
        except OSError:
            pass

        fresh = False
        if cached_payload and cached_type:
            try:
                fresh = time.time() - data_path.stat().st_mtime < max_age
            except OSError:
                pass
        if fresh:
            return cached_payload, cached_type, False

        with IMAGE_SOURCE_LOCK:
            registered = dict(IMAGE_SOURCES.get(key) or {})
        source_url = str(registered.get("url") or metadata.get("source_url") or "").strip()
        if source_url and allowed_image_url(source_url):
            try:
                payload, content_type = fetch_remote_image(source_url, timeout, max_bytes)
                cache_dir.mkdir(parents=True, exist_ok=True)
                tmp_data = data_path.with_suffix(".image.tmp")
                tmp_meta = meta_path.with_suffix(".json.tmp")
                tmp_data.write_bytes(payload)
                tmp_meta.write_text(
                    json.dumps(
                        {
                            "source_url": source_url,
                            "content_type": content_type,
                            "updated_at": datetime.now(timezone.utc).isoformat(),
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                tmp_data.replace(data_path)
                tmp_meta.replace(meta_path)
                return payload, content_type, False
            except Exception as exc:
                LOGGER.warning("Pokémon-Bild %s konnte nicht aktualisiert werden: %s", key, exc)

        if cached_payload and cached_type:
            return cached_payload, cached_type, True
        return None

def parse_source_datetime(value: Any, local_tz: ZoneInfo) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None

    raw = value.strip()
    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        LOGGER.warning("Ungültiger Zeitstempel wird übersprungen: %s", raw)
        return None

    # ScrapedDuck: timestamps ending in Z are global UTC times; timestamps
    # without an offset are local event times.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=local_tz)

    return parsed


def normalized_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip().casefold() for item in value if str(item).strip()]


def event_matches(event: dict[str, Any], config: dict[str, Any], now_local: datetime) -> bool:
    filters = config.get("filters", {})
    include_types = set(normalized_string_list(filters.get("include_types")))
    exclude_types = set(normalized_string_list(filters.get("exclude_types")))
    include_keywords = normalized_string_list(filters.get("include_keywords"))
    exclude_keywords = normalized_string_list(filters.get("exclude_keywords"))

    event_type = str(event.get("eventType") or "").strip().casefold()
    if include_types and event_type not in include_types:
        return False
    if event_type in exclude_types:
        return False
    global_only = set(normalized_string_list(filters.get("global_only_types")))
    if event_type in global_only and "global" not in str(event.get("name") or "").casefold():
        return False

    searchable = " ".join(
        str(event.get(field) or "") for field in ("name", "heading", "eventType", "eventID")
    ).casefold()
    if include_keywords and not any(keyword in searchable for keyword in include_keywords):
        return False
    if any(keyword in searchable for keyword in exclude_keywords):
        return False

    generic = event.get("extraData") or {}
    if isinstance(generic, dict):
        generic = generic.get("generic") or {}
    if not isinstance(generic, dict):
        generic = {}

    require_spawns = filters.get("require_spawns")
    if isinstance(require_spawns, bool) and bool(generic.get("hasSpawns")) != require_spawns:
        return False

    require_research = filters.get("require_field_research")
    if isinstance(require_research, bool) and bool(generic.get("hasFieldResearchTasks")) != require_research:
        return False

    local_tz = now_local.tzinfo
    start = parse_source_datetime(event.get("start"), local_tz)  # type: ignore[arg-type]
    end = parse_source_datetime(event.get("end"), local_tz)  # type: ignore[arg-type]
    if start is None or end is None:
        return False

    past_days = int(filters.get("past_days", 3))
    future_days = int(filters.get("future_days", 365))
    lower_bound = now_local - timedelta(days=max(past_days, 0))
    upper_bound = now_local + timedelta(days=max(future_days, 0))

    # Keep events that overlap the configured time window.
    return end >= lower_bound and start <= upper_bound


def ical_escape(value: Any) -> str:
    text = str(value or "")
    return (
        text.replace("\\", "\\\\")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\n", "\\n")
        .replace(";", "\\;")
        .replace(",", "\\,")
    )


def fold_ical_line(line: str) -> str:
    """Fold an iCalendar content line to at most 75 UTF-8 octets."""
    chunks: list[str] = []
    current = ""
    current_bytes = 0
    limit = 75

    for char in line:
        char_bytes = len(char.encode("utf-8"))
        if current and current_bytes + char_bytes > limit:
            chunks.append(current)
            current = char
            current_bytes = char_bytes
            limit = 74  # Continuation line gets one leading space.
        else:
            current += char
            current_bytes += char_bytes

    if current or not chunks:
        chunks.append(current)

    return "\r\n ".join(chunks)


def format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build_description(
    event: dict[str, Any],
    config: dict[str, Any],
    pokemon_mapping: dict[str, str],
    pokemon_pattern: re.Pattern[str] | None,
) -> str:
    lines: list[str] = []
    heading = translate_text(event.get("heading"), config, pokemon_mapping, pokemon_pattern).strip()
    if heading:
        lines.append(heading)

    extra = event.get("extraData")
    if isinstance(extra, dict):
        spotlight = extra.get("spotlight")
        if isinstance(spotlight, dict):
            bonus = translate_text(spotlight.get("bonus"), config, pokemon_mapping, pokemon_pattern).strip()
            if bonus:
                lines.append(f"Bonus: {bonus}")

        raids = extra.get("raidbattles")
        if isinstance(raids, dict):
            bosses = raids.get("bosses")
            if isinstance(bosses, list):
                names = [
                    translate_text(b.get("name"), config, pokemon_mapping, pokemon_pattern)
                    for b in bosses
                    if isinstance(b, dict) and b.get("name")
                ]
                if names:
                    lines.append("Raid-Bosse: " + ", ".join(names))

        community_day = extra.get("communityday")
        if isinstance(community_day, dict):
            bonuses = community_day.get("bonuses")
            if isinstance(bonuses, list):
                texts = [
                    translate_text(b.get("text"), config, pokemon_mapping, pokemon_pattern)
                    for b in bonuses
                    if isinstance(b, dict) and b.get("text")
                ]
                if texts:
                    lines.append("Boni: " + " | ".join(texts))

    link = str(event.get("link") or "").strip()
    if link:
        lines.append(f"Details: {link}")
    lines.append("Daten: LeekDuck.com via ScrapedDuck")
    return "\n".join(lines)


def build_manual_description(event: dict[str, Any]) -> str:
    lines: list[str] = []
    description = str(event.get("description") or "").strip()
    if description:
        lines.append(description)
    location = str(event.get("location") or "").strip()
    if location:
        lines.append(f"Ort: {location}")
    link = str(event.get("url") or "").strip()
    if link:
        lines.append(f"Details: {link}")
    return "\n".join(lines)


def create_calendar(
    events: list[dict[str, Any]],
    manual_events: list[dict[str, Any]],
    config: dict[str, Any],
) -> tuple[bytes, int, list[dict[str, Any]], int]:
    local_tz = ZoneInfo(str(config["timezone"]))
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(local_tz)

    selected = [event for event in events if event_matches(event, config, now_local)]
    selected.sort(key=lambda event: str(event.get("start") or ""))
    selected_manual = [event for event in manual_events if manual_event_in_window(event, config, now_local)]
    selected_manual.sort(key=lambda event: str(event.get("start") or ""))
    pokemon_mapping, pokemon_pattern = get_pokemon_translator(config)

    calendar_name = str(config.get("calendar_name") or DEFAULT_CONFIG["calendar_name"])
    description = str(config.get("calendar_description") or DEFAULT_CONFIG["calendar_description"])

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Self-hosted//ScrapedDuck iCal//DE",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{ical_escape(calendar_name)}",
        f"X-WR-CALDESC:{ical_escape(description)}",
        f"X-WR-TIMEZONE:{ical_escape(config['timezone'])}",
        "REFRESH-INTERVAL;VALUE=DURATION:PT30M",
        "X-PUBLISHED-TTL:PT30M",
    ]
    web_events: list[dict[str, Any]] = []

    for event in selected:
        start = parse_source_datetime(event.get("start"), local_tz)
        end = parse_source_datetime(event.get("end"), local_tz)
        if start is None or end is None:
            continue

        event_id = str(event.get("eventID") or hashlib.sha256(json.dumps(event, sort_keys=True).encode()).hexdigest())
        link = str(event.get("link") or "").strip()
        name = translate_text(event.get("name") or "Pokémon GO Event", config, pokemon_mapping, pokemon_pattern).strip()
        event_type = str(event.get("eventType") or "event").strip()
        category = EVENT_TYPE_LABELS.get(event_type.casefold(), event_type)
        event_description = build_description(event, config, pokemon_mapping, pokemon_pattern)
        heading = translate_text(event.get("heading"), config, pokemon_mapping, pokemon_pattern).strip()
        pokemon_images = event_pokemon_images(event, config, pokemon_mapping, pokemon_pattern)

        lines.extend([
            "BEGIN:VEVENT",
            f"UID:{ical_escape(event_id)}@scrapedduck-ical",
            f"DTSTAMP:{format_utc(now_utc)}",
            f"LAST-MODIFIED:{format_utc(now_utc)}",
            "SEQUENCE:3",
            f"DTSTART:{format_utc(start)}",
            f"DTEND:{format_utc(end)}",
            f"SUMMARY:{ical_escape(name)}",
            f"DESCRIPTION:{ical_escape(event_description)}",
            f"CATEGORIES:{ical_escape(category)}",
            "STATUS:CONFIRMED",
            "TRANSP:TRANSPARENT",
        ])
        if link:
            lines.append(f"URL:{ical_escape(link)}")
        lines.append("END:VEVENT")

        web_events.append({
            "id": event_id,
            "title": name,
            "sourceTitle": str(event.get("name") or ""),
            "start": start.astimezone(local_tz).isoformat(),
            "end": end.astimezone(local_tz).isoformat(),
            "url": link,
            "description": event_description,
            "heading": heading,
            "category": category,
            "eventType": event_type,
            "pokemonImages": pokemon_images,
            "allDay": False,
            "isManual": False,
            "location": "",
        })

    for event in selected_manual:
        try:
            start, end, all_day = manual_event_datetimes(event, local_tz)
        except ValueError as exc:
            LOGGER.warning("%s", exc)
            continue
        event_id = str(event.get("id") or f"manual-{secrets.token_hex(8)}")
        title = str(event.get("title") or "Manuelles Event").strip()
        event_type = str(event.get("type") or "manual-event").strip().casefold()
        category = EVENT_TYPE_LABELS.get(event_type, event_type.replace("-", " ").title())
        event_description = build_manual_description(event)
        location = str(event.get("location") or "").strip()
        link = str(event.get("url") or "").strip()
        image_source_event = {
            "eventType": event_type,
            "name": title,
            "pokemon": event.get("pokemon") or [],
        }
        pokemon_images = event_pokemon_images(image_source_event, config, pokemon_mapping, pokemon_pattern)

        updated_at = parse_source_datetime(event.get("updated_at"), local_tz) or now_utc
        lines.extend([
            "BEGIN:VEVENT",
            f"UID:{ical_escape(event_id)}@scrapedduck-ical",
            f"DTSTAMP:{format_utc(now_utc)}",
            f"LAST-MODIFIED:{format_utc(updated_at)}",
            "SEQUENCE:1",
        ])
        if all_day:
            lines.append(f"DTSTART;VALUE=DATE:{start.strftime('%Y%m%d')}")
            lines.append(f"DTEND;VALUE=DATE:{end.strftime('%Y%m%d')}")
        else:
            lines.append(f"DTSTART:{format_utc(start)}")
            lines.append(f"DTEND:{format_utc(end)}")
        lines.extend([
            f"SUMMARY:{ical_escape(title)}",
            f"DESCRIPTION:{ical_escape(event_description)}",
            f"CATEGORIES:{ical_escape(category)}",
            "STATUS:CONFIRMED",
            "TRANSP:TRANSPARENT",
        ])
        if location:
            lines.append(f"LOCATION:{ical_escape(location)}")
        if link:
            lines.append(f"URL:{ical_escape(link)}")
        lines.append("END:VEVENT")

        web_events.append({
            "id": event_id,
            "title": title,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "url": link,
            "description": event_description,
            "heading": "Manuell hinzugefügt",
            "category": category,
            "eventType": event_type,
            "pokemonImages": pokemon_images,
            "allDay": all_day,
            "isManual": True,
            "location": location,
        })

    web_events.sort(key=lambda item: str(item.get("start") or ""))
    lines.append("END:VCALENDAR")
    body = ("\r\n".join(fold_ical_line(line) for line in lines) + "\r\n").encode("utf-8")
    return body, len(web_events), web_events, len(selected_manual)


def get_calendar() -> tuple[bytes, str, dict[str, Any], bool, list[dict[str, Any]]]:
    config = load_config()
    max_age = max(int(config.get("cache_seconds", 1800)), 60)
    now_monotonic = time.monotonic()

    with CACHE_LOCK:
        body = CACHE.get("body")
        if body is not None and now_monotonic - float(CACHE["created_monotonic"]) < max_age:
            return body, str(CACHE["etag"]), config, bool(CACHE.get("stale")), list(CACHE.get("web_events") or [])

        attempt_at = datetime.now(timezone.utc)
        CACHE["last_attempt_at"] = attempt_at.isoformat()
        manual_events = load_manual_events(config)
        CACHE["manual_updated_at"] = manual_file_updated_at(config)
        try:
            events = fetch_events(config)
            body, filtered_count, web_events, manual_count = create_calendar(events, manual_events, config)
            etag = '"' + hashlib.sha256(body).hexdigest() + '"'
            CACHE.update({
                "created_monotonic": now_monotonic,
                "body": body,
                "etag": etag,
                "source_count": len(events),
                "filtered_count": filtered_count,
                "stale": False,
                "last_error": None,
                "web_events": web_events,
                "last_success_at": datetime.now(timezone.utc).isoformat(),
                "source_events": events,
                "manual_count": manual_count,
            })
            LOGGER.info("Kalender aktualisiert: %d Events (%d manuell) aus %d Quell-Events", filtered_count, manual_count, len(events))
            return body, etag, config, False, web_events
        except Exception as exc:
            CACHE["last_error"] = str(exc)
            LOGGER.exception("Kalenderaktualisierung fehlgeschlagen")
            cached_source = list(CACHE.get("source_events") or [])
            if cached_source:
                body, filtered_count, web_events, manual_count = create_calendar(cached_source, manual_events, config)
                etag = '"' + hashlib.sha256(body).hexdigest() + '"'
                CACHE.update({
                    "created_monotonic": now_monotonic,
                    "body": body,
                    "etag": etag,
                    "filtered_count": filtered_count,
                    "web_events": web_events,
                    "manual_count": manual_count,
                    "stale": True,
                })
                return body, etag, config, True, web_events
            if CACHE.get("body") is not None:
                CACHE["created_monotonic"] = now_monotonic - max_age + 60
                CACHE["stale"] = True
                return CACHE["body"], str(CACHE["etag"]), config, True, list(CACHE.get("web_events") or [])
            raise


def build_template_page(template_file: Path, config: dict[str, Any], host: str, page_name: str) -> bytes:
    try:
        template = template_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"{page_name} kann nicht geladen werden: {exc}") from exc

    calendar_path = str(config.get("calendar_path") or "/pokemon-go-de.ics")
    calendar_name = str(config.get("calendar_name") or "Pokémon GO – Events")
    forwarded_host = host.split(",", 1)[0].strip()
    public_host = forwarded_host or "localhost"
    webcal_url = f"webcal://{public_host}{calendar_path}"

    replacements = {
        "__CALENDAR_NAME__": html.escape(calendar_name),
        "__CALENDAR_PATH__": html.escape(calendar_path, quote=True),
        "__WEBCAL_URL__": html.escape(webcal_url, quote=True),
    }
    for marker, value in replacements.items():
        template = template.replace(marker, value)
    return template.encode("utf-8")


def build_landing_page(config: dict[str, Any], host: str) -> bytes:
    return build_template_page(LANDING_FILE, config, host, "Landingpage")


def build_status_page(config: dict[str, Any], host: str) -> bytes:
    return build_template_page(STATUS_FILE, config, host, "Statusseite")


def build_admin_page(config: dict[str, Any], host: str) -> bytes:
    return build_template_page(ADMIN_FILE, config, host, "Adminseite")


def build_health_payload(config: dict[str, Any]) -> dict[str, Any]:
    last_success_at = CACHE.get("last_success_at")
    try:
        manual_total = len(load_manual_events(config))
    except Exception:
        manual_total = int(CACHE.get("manual_count", 0))
    data_age: float | None = None
    if isinstance(last_success_at, str):
        try:
            data_age = max(0.0, (datetime.now(timezone.utc) - datetime.fromisoformat(last_success_at)).total_seconds())
        except ValueError:
            data_age = None
    has_data = CACHE.get("body") is not None
    last_error = CACHE.get("last_error")
    stale = bool(CACHE.get("stale"))
    if not has_data and not last_error:
        status = "starting"
    elif last_error or stale:
        status = "degraded"
    else:
        status = "ok"

    return {
        "status": status,
        "source_events": CACHE.get("source_count", 0),
        "calendar_events": CACHE.get("filtered_count", 0),
        "manual_events": manual_total,
        "manual_events_updated_at": CACHE.get("manual_updated_at") or manual_file_updated_at(config),
        "admin_enabled": bool(manual_config(config).get("enabled", True)),
        "stale": stale,
        "last_error": last_error,
        "last_success_at": CACHE.get("last_success_at"),
        "last_attempt_at": CACHE.get("last_attempt_at"),
        "cache_age_seconds": round(data_age, 1) if data_age is not None else None,
        "cache_ttl_seconds": max(int(config.get("cache_seconds", 1800)), 60),
        "pokemon_name_mappings": len(POKEMON_NAMES_CACHE.get("mapping") or {}),
        "pokemon_mapping_source": POKEMON_NAMES_CACHE.get("source"),
        "pokemon_image_sources": len(IMAGE_SOURCES),
        "pokemon_image_cache_files": sum(1 for _ in Path(str((config.get("pokemon_images") or {}).get("cache_dir") or "/data/pokemon-images")).glob("*.image")),
        "timezone": config.get("timezone"),
        "calendar_path": config.get("calendar_path"),
        "source_url": config.get("source_url"),
        "service_version": SERVICE_VERSION,
        "infographics": infographic_health(config),
    }


def infographic_health(config: dict[str, Any]) -> dict[str, Any]:
    try:
        return INFOGRAPHICS.health(config)
    except Exception as exc:
        return {"enabled": True, "error": str(exc)}


def events_with_infographics(
    config: dict[str, Any], web_events: list[dict[str, Any]], events_key: str | None
) -> list[dict[str, Any]]:
    """Attach matched infographics; any failure leaves the calendar untouched."""
    try:
        by_event = INFOGRAPHICS.refresh(config, web_events, events_key, pokemon_species_matches)
        return INFOGRAPHICS.augment(web_events, by_event)
    except Exception:
        LOGGER.exception("Infografiken konnten nicht zugeordnet werden")
        return web_events


def current_web_events() -> list[dict[str, Any]]:
    try:
        return get_calendar()[4]
    except Exception as exc:
        LOGGER.warning("Events für die Infografik-Verwaltung nicht verfügbar: %s", exc)
        return list(CACHE.get("web_events") or [])


def build_api_payload(config: dict[str, Any], stale: bool, web_events: list[dict[str, Any]]) -> dict[str, Any]:
    settings = infographic_store.infographic_settings(config)
    return {
        "events": web_events,
        "meta": {
            "infographics": {
                "enabled": settings["enabled"],
                "attribution": settings["attribution"],
            },
            "updatedAt": CACHE.get("last_success_at"),
            "lastAttemptAt": CACHE.get("last_attempt_at"),
            "stale": stale,
            "sourceCount": CACHE.get("source_count", 0),
            "calendarCount": CACHE.get("filtered_count", 0),
            "manualCount": CACHE.get("manual_count", 0),
            "manualUpdatedAt": CACHE.get("manual_updated_at") or manual_file_updated_at(config),
            "timezone": config.get("timezone"),
            "cacheSeconds": max(int(config.get("cache_seconds", 1800)), 60),
        },
    }


class CalendarHandler(BaseHTTPRequestHandler):
    server_version = f"ScrapedDuck-iCal/{SERVICE_VERSION}"

    def log_message(self, fmt: str, *args: Any) -> None:
        LOGGER.info("%s - %s", self.address_string(), fmt % args)

    def send_bytes(self, status: int, body: bytes, content_type: str, extra_headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if extra_headers:
            for key, value in extra_headers.items():
                self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, status: int, payload: Any, extra_headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_bytes(status, body, "application/json; charset=utf-8", extra_headers)

    def read_json_body(self, max_bytes: int = 131072) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError as exc:
            raise ValueError("Ungültige Content-Length.") from exc
        if length <= 0 or length > max_bytes:
            raise ValueError("Der Request-Body fehlt oder ist zu groß.")
        payload = self.rfile.read(length)
        try:
            data = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Der Request enthält kein gültiges JSON.") from exc
        if not isinstance(data, dict):
            raise ValueError("Der Request muss ein JSON-Objekt enthalten.")
        return data

    def authenticated_session(self) -> tuple[str | None, dict[str, Any] | None]:
        return session_from_headers(self.headers)

    def require_admin(self, require_csrf: bool = False) -> tuple[str, dict[str, Any]] | None:
        token, session = self.authenticated_session()
        if not token or not session:
            self.send_json(401, {"error": "Nicht angemeldet."}, {"Cache-Control": "no-store"})
            return None
        if require_csrf:
            supplied = str(self.headers.get("X-CSRF-Token") or "")
            expected = str(session.get("csrf") or "")
            if not supplied or not hmac.compare_digest(supplied, expected):
                self.send_json(403, {"error": "Ungültiges Sicherheitstoken."}, {"Cache-Control": "no-store"})
                return None
        return token, session

    def admin_cookie(self, token: str, config: dict[str, Any], clear: bool = False) -> str:
        settings = manual_config(config)
        hours = max(1, min(int(settings.get("session_hours", 12)), 168))
        parts = [f"{ADMIN_COOKIE_NAME}={'' if clear else token}", "Path=/", "HttpOnly", "SameSite=Strict"]
        parts.append("Max-Age=0" if clear else f"Max-Age={hours * 3600}")
        if bool(settings.get("secure_cookie", True)):
            parts.append("Secure")
        return "; ".join(parts)

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        path = urlsplit(self.path).path

        try:
            initial_config = load_config()
        except Exception as exc:
            payload = f"Konfiguration derzeit nicht verfügbar: {exc}\n".encode("utf-8")
            self.send_bytes(503, payload, "text/plain; charset=utf-8")
            return

        if path in STATIC_ASSETS:
            asset_path, content_type = STATIC_ASSETS[path]
            try:
                payload = asset_path.read_bytes()
            except OSError:
                self.send_bytes(404, b"Not found\n", "text/plain; charset=utf-8")
                return
            self.send_bytes(
                200,
                payload,
                content_type,
                {"Cache-Control": "public, max-age=604800, immutable"},
            )
            return

        if path.startswith("/pokemon-image/"):
            key = path.rsplit("/", 1)[-1]
            image = load_pokemon_image(key, initial_config)
            if image is None:
                self.send_bytes(404, b"Image not found\n", "text/plain; charset=utf-8")
                return
            payload, content_type, stale_image = image
            headers = {"Cache-Control": "public, max-age=86400, stale-if-error=604800"}
            if stale_image:
                headers["Warning"] = '110 - "Stale Pokemon image served"'
            self.send_bytes(200, payload, content_type, headers)
            return

        if path.startswith("/infographic/"):
            record_id = path.rsplit("/", 1)[-1]
            _, session = self.authenticated_session()
            try:
                image = INFOGRAPHICS.image_file(initial_config, record_id, is_admin=bool(session))
            except Exception:
                LOGGER.exception("Infografik %s konnte nicht gelesen werden", record_id)
                image = None
            if image is None:
                self.send_bytes(404, b"Image not found\n", "text/plain; charset=utf-8", {"Cache-Control": "no-store"})
                return
            payload, content_type = image
            cache = "private, no-store" if session else "public, max-age=86400"
            self.send_bytes(200, payload, content_type, {"Cache-Control": cache})
            return

        if path == "/api/admin/infographics":
            if not self.require_admin():
                return
            try:
                result = INFOGRAPHICS.admin_payload(initial_config, current_web_events())
            except Exception as exc:
                LOGGER.exception("Infografik-Übersicht fehlgeschlagen")
                self.send_json(500, {"error": str(exc)}, {"Cache-Control": "no-store"})
                return
            self.send_json(200, result, {"Cache-Control": "no-store"})
            return

        if path == "/health":
            payload = json.dumps(build_health_payload(initial_config), ensure_ascii=False).encode("utf-8")
            self.send_bytes(200, payload, "application/json; charset=utf-8", {"Cache-Control": "no-store"})
            return

        if path == "/status":
            try:
                status_page = build_status_page(initial_config, self.headers.get("X-Forwarded-Host") or self.headers.get("Host", ""))
            except Exception as exc:
                payload = f"Statusseite derzeit nicht verfügbar: {exc}\n".encode("utf-8")
                self.send_bytes(503, payload, "text/plain; charset=utf-8")
                return
            self.send_bytes(
                200,
                status_page,
                "text/html; charset=utf-8",
                {"Cache-Control": "no-cache, max-age=0, must-revalidate"},
            )
            return

        if path == "/admin":
            try:
                admin_page = build_admin_page(initial_config, self.headers.get("X-Forwarded-Host") or self.headers.get("Host", ""))
            except Exception as exc:
                self.send_bytes(503, f"Adminseite derzeit nicht verfügbar: {exc}\n".encode("utf-8"), "text/plain; charset=utf-8")
                return
            self.send_bytes(200, admin_page, "text/html; charset=utf-8", {
                "Cache-Control": "no-store",
                "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; base-uri 'none'; form-action 'self'",
            })
            return

        if path == "/api/admin/session":
            _, session = self.authenticated_session()
            self.send_json(200, {
                "authenticated": bool(session),
                "csrf": session.get("csrf") if session else None,
                "passwordIsDefault": bool(session) and hmac.compare_digest(admin_password(initial_config), DEFAULT_ADMIN_PASSWORD),
            }, {"Cache-Control": "no-store"})
            return

        if path == "/api/admin/events":
            if not self.require_admin():
                return
            try:
                events = [manual_event_for_admin(item) for item in load_manual_events(initial_config)]
            except Exception as exc:
                self.send_json(500, {"error": str(exc)}, {"Cache-Control": "no-store"})
                return
            self.send_json(200, {"events": events}, {"Cache-Control": "no-store"})
            return

        try:
            body, etag, config, stale, web_events = get_calendar()
        except Exception as exc:
            payload = f"Kalender derzeit nicht verfügbar: {exc}\n".encode("utf-8")
            self.send_bytes(503, payload, "text/plain; charset=utf-8", {"Retry-After": "60"})
            return

        if path == "/":
            try:
                landing = build_landing_page(config, self.headers.get("X-Forwarded-Host") or self.headers.get("Host", ""))
            except Exception as exc:
                payload = f"Landingpage derzeit nicht verfügbar: {exc}\n".encode("utf-8")
                self.send_bytes(503, payload, "text/plain; charset=utf-8")
                return
            self.send_bytes(
                200,
                landing,
                "text/html; charset=utf-8",
                {"Cache-Control": "no-cache, max-age=0, must-revalidate"},
            )
            return

        if path == "/api/events":
            api_payload = build_api_payload(config, stale, events_with_infographics(config, web_events, etag))
            try:
                api_payload["meta"]["featured"] = INFOGRAPHICS.featured(config)
            except Exception:
                LOGGER.exception("Allgemeine Infografiken konnten nicht ermittelt werden")
                api_payload["meta"]["featured"] = {"weekly": [], "rocket": None, "news": []}
            api_body = json.dumps(api_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            api_etag = '"' + hashlib.sha256(api_body).hexdigest() + '"'
            if self.headers.get("If-None-Match") == api_etag:
                self.send_response(304)
                self.send_header("ETag", api_etag)
                self.end_headers()
                return
            self.send_bytes(
                200,
                api_body,
                "application/json; charset=utf-8",
                {"Cache-Control": "no-cache, max-age=0, must-revalidate", "ETag": api_etag},
            )
            return

        if path != config["calendar_path"]:
            self.send_bytes(404, b"Not found\n", "text/plain; charset=utf-8")
            return

        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.end_headers()
            return

        headers = {
            "Content-Disposition": 'inline; filename="pokemon-go.ics"',
            "Cache-Control": "no-cache, max-age=0, must-revalidate",
            "ETag": etag,
        }
        if stale:
            headers["Warning"] = '110 - "Stale calendar data served"'
        self.send_bytes(200, body, "text/calendar; charset=utf-8", headers)


    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        try:
            config = load_config()
        except Exception as exc:
            self.send_json(503, {"error": f"Konfiguration nicht verfügbar: {exc}"})
            return

        if path == "/api/admin/login":
            ip = client_ip(self.headers, self.client_address[0])
            allowed, retry_after = login_allowed(ip, config)
            if not allowed:
                self.send_json(429, {"error": "Zu viele fehlgeschlagene Versuche. Bitte später erneut versuchen."}, {
                    "Retry-After": str(retry_after), "Cache-Control": "no-store"
                })
                return
            try:
                payload = self.read_json_body(8192)
            except ValueError as exc:
                self.send_json(400, {"error": str(exc)}, {"Cache-Control": "no-store"})
                return
            supplied = str(payload.get("password") or "")
            if not hmac.compare_digest(supplied.encode("utf-8"), admin_password(config).encode("utf-8")):
                record_login_failure(ip)
                time.sleep(0.35)
                self.send_json(401, {"error": "Passwort ist nicht korrekt."}, {"Cache-Control": "no-store"})
                return
            clear_login_failures(ip)
            token, session = create_admin_session(config)
            self.send_json(200, {
                "authenticated": True,
                "csrf": session["csrf"],
                "passwordIsDefault": hmac.compare_digest(admin_password(config), DEFAULT_ADMIN_PASSWORD),
            }, {"Set-Cookie": self.admin_cookie(token, config), "Cache-Control": "no-store"})
            return

        if path == "/api/admin/logout":
            auth = self.require_admin(require_csrf=True)
            if not auth:
                return
            token, _ = auth
            with SESSIONS_LOCK:
                ADMIN_SESSIONS.pop(token, None)
            self.send_json(200, {"ok": True}, {
                "Set-Cookie": self.admin_cookie("", config, clear=True), "Cache-Control": "no-store"
            })
            return

        if path == "/api/admin/events":
            if not self.require_admin(require_csrf=True):
                return
            try:
                payload = self.read_json_body()
                events = load_manual_events(config)
                event = validate_manual_event_payload(payload, config)
                events.append(event)
                save_manual_events(config, events)
                invalidate_calendar_cache()
                self.send_json(201, {"event": manual_event_for_admin(event)}, {"Cache-Control": "no-store"})
            except ValueError as exc:
                self.send_json(400, {"error": str(exc)}, {"Cache-Control": "no-store"})
            except Exception as exc:
                LOGGER.exception("Manuelles Event konnte nicht gespeichert werden")
                self.send_json(500, {"error": str(exc)}, {"Cache-Control": "no-store"})
            return

        if path.startswith("/api/admin/infographics/"):
            self.handle_infographic_action(path, config)
            return

        self.send_json(404, {"error": "Nicht gefunden."}, {"Cache-Control": "no-store"})

    def handle_infographic_action(self, path: str, config: dict[str, Any]) -> None:
        if not self.require_admin(require_csrf=True):
            return
        headers = {"Cache-Control": "no-store"}
        parts = path[len("/api/admin/infographics/"):].strip("/").split("/")
        try:
            payload = self.read_json_body(8192) if int(self.headers.get("Content-Length") or "0") > 0 else {}
        except ValueError as exc:
            self.send_json(400, {"error": str(exc)}, headers)
            return
        try:
            if parts == ["cleanup"]:
                mode = str(payload.get("mode") or "")
                if mode not in ("rejected", "older"):
                    raise ValueError("Unbekannter Aufräummodus.")
                days = max(1, min(int(payload.get("older_than_days") or 120), 3650))
                removed = INFOGRAPHICS.cleanup(config, mode, days)
                self.send_json(200, {"ok": True, "removed": removed}, headers)
                return
            if len(parts) != 2:
                raise KeyError(path)
            record_id, action = parts
            if action == "assign":
                event_id = sanitize_text(payload.get("event_id"), 300, "Event", required=True)
                events = {str(event.get("id")): event for event in current_web_events()}
                if event_id not in events:
                    try:
                        manual = {str(event.get("id")): event for event in load_manual_events(config)}
                    except Exception:
                        manual = {}
                    if event_id not in manual:
                        raise ValueError("Das ausgewählte Event existiert nicht im Kalender.")
                    title = str(manual[event_id].get("title") or event_id)
                else:
                    title = str(events[event_id].get("title") or event_id)
                item = INFOGRAPHICS.assign(config, record_id, event_id, title)
            elif action == "category":
                category = sanitize_text(payload.get("category"), 20, "Kategorie", required=True)
                item = INFOGRAPHICS.set_category(config, record_id, category)
                INFOGRAPHICS.refresh(config, current_web_events(), None, pokemon_species_matches)
                item = INFOGRAPHICS.store(config).get(record_id) or item
            elif action == "unassign":
                item = INFOGRAPHICS.unassign(config, record_id)
            elif action == "reject":
                item = INFOGRAPHICS.reject(config, record_id)
            elif action == "rematch":
                item = INFOGRAPHICS.rematch(config, record_id)
                INFOGRAPHICS.refresh(config, current_web_events(), None, pokemon_species_matches)
                item = INFOGRAPHICS.store(config).get(record_id) or item
            else:
                raise KeyError(action)
            self.send_json(200, {"ok": True, "status": (item.get("matching") or {}).get("status")}, headers)
        except KeyError:
            self.send_json(404, {"error": "Infografik oder Aktion nicht gefunden."}, headers)
        except ValueError as exc:
            self.send_json(400, {"error": str(exc)}, headers)
        except Exception as exc:
            LOGGER.exception("Infografik-Aktion fehlgeschlagen")
            self.send_json(500, {"error": str(exc)}, headers)

    def do_PUT(self) -> None:
        path = urlsplit(self.path).path
        if not path.startswith("/api/admin/events/"):
            self.send_json(404, {"error": "Nicht gefunden."}, {"Cache-Control": "no-store"})
            return
        try:
            config = load_config()
        except Exception as exc:
            self.send_json(503, {"error": f"Konfiguration nicht verfügbar: {exc}"})
            return
        if not self.require_admin(require_csrf=True):
            return
        event_id = path.rsplit("/", 1)[-1]
        try:
            payload = self.read_json_body()
            events = load_manual_events(config)
            index = next((i for i, item in enumerate(events) if str(item.get("id")) == event_id), None)
            if index is None:
                self.send_json(404, {"error": "Event nicht gefunden."}, {"Cache-Control": "no-store"})
                return
            updated = validate_manual_event_payload(payload, config, events[index])
            events[index] = updated
            save_manual_events(config, events)
            invalidate_calendar_cache()
            self.send_json(200, {"event": manual_event_for_admin(updated)}, {"Cache-Control": "no-store"})
        except ValueError as exc:
            self.send_json(400, {"error": str(exc)}, {"Cache-Control": "no-store"})
        except Exception as exc:
            LOGGER.exception("Manuelles Event konnte nicht aktualisiert werden")
            self.send_json(500, {"error": str(exc)}, {"Cache-Control": "no-store"})

    def do_DELETE(self) -> None:
        path = urlsplit(self.path).path
        if path.startswith("/api/admin/infographics/"):
            try:
                config = load_config()
            except Exception as exc:
                self.send_json(503, {"error": f"Konfiguration nicht verfügbar: {exc}"})
                return
            if not self.require_admin(require_csrf=True):
                return
            record_id = path.rsplit("/", 1)[-1]
            try:
                removed = INFOGRAPHICS.delete(config, [record_id]) if infographic_store.valid_record_id(record_id) else 0
            except Exception as exc:
                LOGGER.exception("Infografik konnte nicht gelöscht werden")
                self.send_json(500, {"error": str(exc)}, {"Cache-Control": "no-store"})
                return
            if not removed:
                self.send_json(404, {"error": "Infografik nicht gefunden."}, {"Cache-Control": "no-store"})
                return
            self.send_json(200, {"ok": True}, {"Cache-Control": "no-store"})
            return
        if not path.startswith("/api/admin/events/"):
            self.send_json(404, {"error": "Nicht gefunden."}, {"Cache-Control": "no-store"})
            return
        try:
            config = load_config()
        except Exception as exc:
            self.send_json(503, {"error": f"Konfiguration nicht verfügbar: {exc}"})
            return
        if not self.require_admin(require_csrf=True):
            return
        event_id = path.rsplit("/", 1)[-1]
        try:
            events = load_manual_events(config)
            remaining = [item for item in events if str(item.get("id")) != event_id]
            if len(remaining) == len(events):
                self.send_json(404, {"error": "Event nicht gefunden."}, {"Cache-Control": "no-store"})
                return
            save_manual_events(config, remaining)
            invalidate_calendar_cache()
            self.send_json(200, {"ok": True}, {"Cache-Control": "no-store"})
        except Exception as exc:
            LOGGER.exception("Manuelles Event konnte nicht gelöscht werden")
            self.send_json(500, {"error": str(exc)}, {"Cache-Control": "no-store"})


def main() -> None:
    config = load_config()
    # Load the mapping during startup so failures and the selected fallback are
    # visible immediately in the container log and /health endpoint.
    get_pokemon_translator(config)
    load_pokemon_species_index()
    ensure_manual_events_file(config)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), CalendarHandler)
    LOGGER.info("Server gestartet auf Port %d; Kalenderpfad: %s", PORT, config["calendar_path"])
    server.serve_forever()


if __name__ == "__main__":
    main()
