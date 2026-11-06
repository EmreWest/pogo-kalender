"""Persistent storage for G47IX infographics.

Both the web server and the Discord worker read and write
``infographic-events.json``. Every change therefore runs inside an exclusive
``fcntl`` lock on a sidecar lock file and is written atomically
(temporary file + ``os.replace``). Only the Python standard library is used.
"""

from __future__ import annotations

import contextlib
import copy
import fcntl
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

STORE_VERSION = 1
RECORD_ID_PATTERN = re.compile(r"[0-9]{5,25}-[0-9a-z]{1,40}")
MATCH_STATUSES = ("pending", "matched", "manual_review", "rejected", "error")
CATEGORIES = ("event", "weekly", "rocket", "news")
MAX_SKIPPED_ENTRIES = 3000
MAX_DELETED_ENTRIES = 5000

DEFAULT_SETTINGS: dict[str, Any] = {
    "enabled": True,
    "storage_dir": "/data/infographics",
    "metadata_file": "/data/infographic-events.json",
    "worker_state_file": "/data/discord-worker-state.json",
    "max_file_size_mb": 15,
    "allowed_extensions": [".jpg", ".jpeg", ".png", ".webp"],
    "allowed_download_hosts": ["cdn.discordapp.com", "media.discordapp.net"],
    "include_embed_images": True,
    "poll_interval_seconds": 300,
    "recheck_recent_messages": 25,
    "download_timeout_seconds": 30,
    "historical_import": {
        "enabled": True,
        "message_limit": 100,
    },
    "ocr": {
        "enabled": True,
        "languages": "deu+eng",
        "timeout_seconds": 90,
        "max_chars": 8000,
    },
    "matching": {
        "auto_match_threshold": 80,
        "manual_review_threshold": 50,
        "min_margin": 15,
        "max_candidates": 5,
        "title_glossary": {
            "twilight": ["zwielicht"],
            "wishes": ["wünsche", "wuensche"],
            "trails": ["pfade", "wege"],
            "showers": ["schauer", "regen"],
            "wild": ["wild", "wilde", "wildes"],
            "area": ["areal", "gebiet"],
            "hatch": ["schlüpf", "schlüpftag"],
            "festival": ["festival", "fest"],
            "harvest": ["ernte"],
            "lights": ["lichter"],
            "holiday": ["feiertag", "feiertage", "festtage"],
            "winter": ["winter"],
            "spring": ["frühling"],
            "summer": ["sommer"],
            "autumn": ["herbst"],
            "fall": ["herbst"],
            "halloween": ["halloween"],
            "anniversary": ["jubiläum"],
            "adventure": ["abenteuer"],
            "dragon": ["drachen"],
            "fairy": ["feen"],
            "ghost": ["geist", "geister"],
            "shadow": ["crypto"],
        },
    },
    "attribution": {
        "creator": "G47IX",
        "text": "Infografik von G47IX – mit freundlicher Genehmigung",
        "source_url": "https://linktr.ee/g47ix",
    },
    "featured": {
        "weekly_lead_days": 3,
        "rocket_max_age_days": 60,
        "news_days": 14,
        "news_max_items": 6,
    },
}

IMAGE_EXTENSIONS_BY_TYPE = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
CONTENT_TYPES_BY_EXTENSION = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _clamp_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(number, maximum))


def infographic_settings(config: dict[str, Any] | None) -> dict[str, Any]:
    """Return the validated ``infographics`` section of the configuration."""
    raw = (config or {}).get("infographics") if isinstance(config, dict) else None
    settings = _deep_merge(DEFAULT_SETTINGS, raw if isinstance(raw, dict) else {})

    settings["enabled"] = bool(settings.get("enabled", True))
    settings["max_file_size_mb"] = _clamp_int(settings.get("max_file_size_mb"), 15, 1, 50)
    settings["poll_interval_seconds"] = _clamp_int(settings.get("poll_interval_seconds"), 300, 60, 86400)
    settings["recheck_recent_messages"] = _clamp_int(settings.get("recheck_recent_messages"), 25, 0, 100)
    settings["download_timeout_seconds"] = _clamp_int(settings.get("download_timeout_seconds"), 30, 5, 120)

    extensions = []
    for item in settings.get("allowed_extensions") or []:
        ext = str(item).strip().casefold()
        if not ext.startswith("."):
            ext = "." + ext
        if ext in CONTENT_TYPES_BY_EXTENSION:
            extensions.append(ext)
    settings["allowed_extensions"] = sorted(set(extensions)) or [".jpg", ".jpeg", ".png", ".webp"]

    hosts = [str(item).strip().casefold() for item in settings.get("allowed_download_hosts") or [] if str(item).strip()]
    settings["allowed_download_hosts"] = sorted(set(hosts)) or ["cdn.discordapp.com", "media.discordapp.net"]

    historical = settings.get("historical_import") if isinstance(settings.get("historical_import"), dict) else {}
    settings["historical_import"] = {
        "enabled": bool(historical.get("enabled", True)),
        "message_limit": _clamp_int(historical.get("message_limit"), 100, 0, 1000),
    }

    ocr = settings.get("ocr") if isinstance(settings.get("ocr"), dict) else {}
    languages = str(ocr.get("languages") or "deu+eng")
    if not re.fullmatch(r"[a-z_]{3,16}(\+[a-z_]{3,16}){0,4}", languages):
        languages = "deu+eng"
    settings["ocr"] = {
        "enabled": bool(ocr.get("enabled", True)),
        "languages": languages,
        "timeout_seconds": _clamp_int(ocr.get("timeout_seconds"), 90, 10, 600),
        "max_chars": _clamp_int(ocr.get("max_chars"), 8000, 500, 50000),
    }

    matching = settings.get("matching") if isinstance(settings.get("matching"), dict) else {}
    auto = _clamp_int(matching.get("auto_match_threshold"), 80, 1, 200)
    review = _clamp_int(matching.get("manual_review_threshold"), 50, 1, 200)
    if review > auto:
        review = auto
    glossary = matching.get("title_glossary") if isinstance(matching.get("title_glossary"), dict) else {}
    clean_glossary: dict[str, list[str]] = {}
    for key, values in glossary.items():
        if isinstance(values, str):
            values = [values]
        if isinstance(values, list):
            clean_glossary[str(key).casefold()] = [str(v).casefold() for v in values if str(v).strip()]
    settings["matching"] = {
        "auto_match_threshold": auto,
        "manual_review_threshold": review,
        "min_margin": _clamp_int(matching.get("min_margin"), 15, 0, 100),
        "max_candidates": _clamp_int(matching.get("max_candidates"), 5, 1, 20),
        "title_glossary": clean_glossary,
    }

    featured = settings.get("featured") if isinstance(settings.get("featured"), dict) else {}
    settings["featured"] = {
        "weekly_lead_days": _clamp_int(featured.get("weekly_lead_days"), 3, 0, 14),
        "rocket_max_age_days": _clamp_int(featured.get("rocket_max_age_days"), 60, 1, 365),
        "news_days": _clamp_int(featured.get("news_days"), 14, 1, 365),
        "news_max_items": _clamp_int(featured.get("news_max_items"), 6, 1, 20),
    }

    attribution = settings.get("attribution") if isinstance(settings.get("attribution"), dict) else {}
    source_url = str(attribution.get("source_url") or "").strip()
    if not source_url.startswith(("https://", "http://")):
        source_url = ""
    settings["attribution"] = {
        "creator": str(attribution.get("creator") or "G47IX")[:80],
        "text": str(attribution.get("text") or "Infografik von G47IX")[:300],
        "source_url": source_url[:500],
    }
    return settings


def detect_image_type(data: bytes) -> str | None:
    """Identify an image by its magic bytes (the server header is not trusted)."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def image_dimensions(data: bytes, content_type: str) -> tuple[int, int] | None:
    """Read width/height from PNG, JPEG or WebP headers without extra libraries."""
    try:
        if content_type == "image/png" and len(data) >= 24:
            return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
        if content_type == "image/jpeg":
            index = 2
            while index + 9 < len(data):
                if data[index] != 0xFF:
                    index += 1
                    continue
                marker = data[index + 1]
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    index += 2
                    continue
                length = int.from_bytes(data[index + 2:index + 4], "big")
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    height = int.from_bytes(data[index + 5:index + 7], "big")
                    width = int.from_bytes(data[index + 7:index + 9], "big")
                    return width, height
                index += 2 + length
        if content_type == "image/webp" and len(data) >= 30:
            chunk = data[12:16]
            if chunk == b"VP8X":
                width = 1 + int.from_bytes(data[24:27], "little")
                height = 1 + int.from_bytes(data[27:30], "little")
                return width, height
            if chunk == b"VP8 ":
                width = int.from_bytes(data[26:28], "little") & 0x3FFF
                height = int.from_bytes(data[28:30], "little") & 0x3FFF
                return width, height
            if chunk == b"VP8L":
                bits = int.from_bytes(data[21:25], "little")
                return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    except (IndexError, ValueError):
        return None
    return None


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_record_id(message_id: str, suffix: str) -> str:
    clean_suffix = re.sub(r"[^0-9a-z]", "", str(suffix).casefold())[:40] or "0"
    record_id = f"{message_id}-{clean_suffix}"
    if not RECORD_ID_PATTERN.fullmatch(record_id):
        raise ValueError(f"Ungültige Infografik-ID: {record_id}")
    return record_id


def valid_record_id(value: str) -> bool:
    return bool(RECORD_ID_PATTERN.fullmatch(str(value or "")))


def empty_payload() -> dict[str, Any]:
    return {"version": STORE_VERSION, "updated_at": None, "items": [], "deleted": [], "skipped": {}}


def normalize_payload(raw: Any) -> dict[str, Any]:
    payload = empty_payload()
    if not isinstance(raw, dict):
        return payload
    items = raw.get("items")
    if isinstance(items, list):
        payload["items"] = [item for item in items if isinstance(item, dict) and valid_record_id(item.get("id"))]
    deleted = raw.get("deleted")
    if isinstance(deleted, list):
        payload["deleted"] = [str(item) for item in deleted if valid_record_id(str(item))][-MAX_DELETED_ENTRIES:]
    skipped = raw.get("skipped")
    if isinstance(skipped, dict):
        payload["skipped"] = {str(k): str(v)[:200] for k, v in skipped.items() if valid_record_id(str(k))}
    payload["updated_at"] = raw.get("updated_at")
    return payload


class InfographicStore:
    """File-backed store shared by the web process and the Discord worker."""

    def __init__(self, metadata_file: str | Path, storage_dir: str | Path) -> None:
        self.metadata_file = Path(metadata_file)
        self.storage_dir = Path(storage_dir)
        self.lock_file = self.metadata_file.with_name(self.metadata_file.name + ".lock")

    @classmethod
    def from_settings(cls, settings: dict[str, Any]) -> "InfographicStore":
        return cls(settings["metadata_file"], settings["storage_dir"])

    @contextlib.contextmanager
    def _locked(self, exclusive: bool) -> Iterator[None]:
        self.metadata_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.lock_file, "a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _read_unlocked(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.metadata_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return empty_payload()
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"{self.metadata_file} kann nicht gelesen werden: {exc}") from exc
        return normalize_payload(raw)

    def _write_unlocked(self, payload: dict[str, Any]) -> None:
        payload = dict(payload)
        payload["version"] = STORE_VERSION
        payload["updated_at"] = utc_now_iso()
        payload["deleted"] = list(dict.fromkeys(payload.get("deleted") or []))[-MAX_DELETED_ENTRIES:]
        skipped = payload.get("skipped") or {}
        if len(skipped) > MAX_SKIPPED_ENTRIES:
            skipped = dict(list(skipped.items())[-MAX_SKIPPED_ENTRIES:])
        payload["skipped"] = skipped
        payload["items"] = sorted(
            payload.get("items") or [],
            key=lambda item: (str((item.get("discord") or {}).get("posted_at") or ""), str(item.get("id"))),
        )
        temporary = self.metadata_file.with_name(self.metadata_file.name + ".tmp")
        data = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        with open(temporary, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            temporary.chmod(0o640)
        except OSError:
            pass
        os.replace(temporary, self.metadata_file)

    def read(self) -> dict[str, Any]:
        with self._locked(exclusive=False):
            return self._read_unlocked()

    def items(self) -> list[dict[str, Any]]:
        return self.read()["items"]

    def get(self, record_id: str) -> dict[str, Any] | None:
        for item in self.items():
            if item.get("id") == record_id:
                return item
        return None

    @contextlib.contextmanager
    def transaction(self) -> Iterator[dict[str, Any]]:
        """Read-modify-write under an exclusive lock.

        The payload is only written when the yielded object was changed, so
        repeated no-op calls do not touch the file's modification time.
        """
        with self._locked(exclusive=True):
            payload = self._read_unlocked()
            before = json.dumps(payload, sort_keys=True, ensure_ascii=False)
            yield payload
            after = json.dumps(payload, sort_keys=True, ensure_ascii=False)
            if after != before:
                self._write_unlocked(payload)

    def mtime(self) -> float:
        try:
            return self.metadata_file.stat().st_mtime
        except OSError:
            return 0.0

    def resolve_file(self, relative_path: str) -> Path | None:
        """Map a stored relative path to a file inside ``storage_dir`` only."""
        if not relative_path:
            return None
        base = self.storage_dir.resolve()
        candidate = (base / str(relative_path)).resolve()
        try:
            candidate.relative_to(base)
        except ValueError:
            return None
        return candidate

    def write_image(self, relative_path: str, data: bytes) -> Path:
        target = self.resolve_file(relative_path)
        if target is None:
            raise ValueError("Ungültiger Speicherpfad für Infografik.")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".tmp")
        with open(temporary, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        return target

    def delete_file(self, relative_path: str) -> bool:
        target = self.resolve_file(relative_path)
        if target is None:
            return False
        try:
            target.unlink()
            return True
        except FileNotFoundError:
            return False

    def file_count(self) -> int:
        try:
            return sum(1 for path in self.storage_dir.rglob("*") if path.is_file() and not path.name.endswith(".tmp"))
        except OSError:
            return 0


def status_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    counts = {status: 0 for status in MATCH_STATUSES}
    for item in items:
        status = str((item.get("matching") or {}).get("status") or "pending")
        counts[status] = counts.get(status, 0) + 1
    return counts


def read_json_file(path: str | Path) -> dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_json_file_atomic(path: str | Path, payload: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)
