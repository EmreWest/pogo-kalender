"""Shared test setup: temporary data directory, configuration and fakes.

Must be imported before ``app`` because ``app`` reads some paths from the
environment at import time. No real Discord credentials are needed.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BASE_DIR = Path(tempfile.mkdtemp(prefix="pogo-tests-"))
CONFIG_PATH = BASE_DIR / "config.json"

os.environ["CONFIG_FILE"] = str(CONFIG_PATH)
os.environ["POKEMON_SPECIES_FILE"] = str(ROOT / "pokemon_species-de.json")
os.environ["LANDING_FILE"] = str(ROOT / "landing.html")
os.environ["STATUS_FILE"] = str(ROOT / "status.html")
os.environ["ADMIN_FILE"] = str(ROOT / "admin.html")
os.environ.pop("ADMIN_PASSWORD", None)
os.environ.setdefault("LOG_LEVEL", "WARNING")

TEST_PASSWORD = "test-passwort-123"

# Minimal valid image headers (the store identifies files by magic bytes).
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + b"\xff\xc0\x00\x11\x08\x07\x8c\x07\x8c\x03" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (1080).to_bytes(4, "big") + (1080).to_bytes(4, "big") + b"\x08\x02\x00\x00\x00" + b"\x00" * 32
GIF = b"GIF89a" + b"\x00" * 40
PDF = b"%PDF-1.7\n" + b"\x00" * 40

GUILD_ID = "111111111111111111"
CHANNEL_ID = "222222222222222222"
WEBHOOK_ID = "333333333333333333"


def make_config(data_dir: Path, **infographic_overrides: Any) -> dict[str, Any]:
    infographics = {
        "enabled": True,
        "storage_dir": str(data_dir / "infographics"),
        "metadata_file": str(data_dir / "infographic-events.json"),
        "worker_state_file": str(data_dir / "discord-worker-state.json"),
        "max_file_size_mb": 1,
        "poll_interval_seconds": 60,
        "recheck_recent_messages": 10,
        "historical_import": {"enabled": True, "message_limit": 50},
        "ocr": {"enabled": False},
    }
    infographics.update(infographic_overrides)
    return {
        "calendar_name": "Pokémon GO – Test",
        "calendar_path": "/pokemon-go-de.ics",
        "timezone": "Europe/Berlin",
        "cache_seconds": 60,
        "translations": {
            "enabled": True,
            "translate_pokemon_names": True,
            "pokemon_names_url": "",
            "pokemon_names_cache_file": str(data_dir / "pokemon_names-de.json"),
            "bundled_pokemon_names_file": str(ROOT / "pokemon_names-de.json"),
        },
        "filters": {"include_types": [], "exclude_types": ["go-battle-league"], "past_days": 3650, "future_days": 3650},
        "pokemon_images": {"enabled": True, "cache_dir": str(data_dir / "pokemon-images")},
        "manual_events": {
            "enabled": True,
            "file": str(data_dir / "manual-events.json"),
            "admin_password": TEST_PASSWORD,
            "secure_cookie": False,
        },
        "infographics": infographics,
    }


def write_config(config: dict[str, Any]) -> None:
    CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")


def fresh_data_dir(name: str) -> Path:
    path = BASE_DIR / name
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    return path


def message(
    message_id: str,
    content: str = "",
    attachments: list[dict[str, Any]] | None = None,
    timestamp: str = "2026-11-03T12:00:00+00:00",
    author_id: str = WEBHOOK_ID,
    webhook: bool = True,
    edited: str | None = None,
    embeds: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    data = {
        "id": message_id,
        "channel_id": CHANNEL_ID,
        "content": content,
        "timestamp": timestamp,
        "edited_timestamp": edited,
        "author": {"id": author_id, "username": "G47IX #infografiken", "bot": webhook},
        "attachments": attachments or [],
        "embeds": embeds or [],
        "message_reference": {"guild_id": "999999999999999999", "channel_id": "888888888888888888", "message_id": "777777777777777777"},
    }
    if webhook:
        data["webhook_id"] = author_id
    return data


def attachment(attachment_id: str, filename: str = "infografik.jpg", size: int = 120000, content_type: str = "image/jpeg") -> dict[str, Any]:
    return {
        "id": attachment_id,
        "filename": filename,
        "size": size,
        "content_type": content_type,
        "url": f"https://cdn.discordapp.com/attachments/{CHANNEL_ID}/{attachment_id}/{filename}?ex=abc&is=def&hm=123",
        "proxy_url": f"https://media.discordapp.net/attachments/{CHANNEL_ID}/{attachment_id}/{filename}",
        "width": 1932,
        "height": 1932,
    }


class FakeDiscordClient:
    """In-memory replacement for DiscordClient (newest-first like Discord)."""

    def __init__(self, messages: list[dict[str, Any]] | None = None, guild_id: str = GUILD_ID) -> None:
        self.messages: list[dict[str, Any]] = list(messages or [])
        self.guild_id = guild_id
        self.fail_with: Exception | None = None
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get_channel(self, channel_id: str) -> dict[str, Any]:
        if self.fail_with:
            raise self.fail_with
        return {"id": channel_id, "guild_id": self.guild_id, "name": "g47ix-infografiken"}

    def get_messages(self, channel_id: str, limit: int = 50, after: str | None = None, before: str | None = None) -> list[dict[str, Any]]:
        self.calls.append(("messages", {"limit": limit, "after": after, "before": before}))
        if self.fail_with:
            raise self.fail_with
        items = sorted(self.messages, key=lambda m: int(m["id"]))
        if after:
            items = [m for m in items if int(m["id"]) > int(after)][:limit]
        elif before:
            items = [m for m in items if int(m["id"]) < int(before)][-limit:]
        else:
            items = items[-limit:]
        return [copy.deepcopy(m) for m in reversed(items)]


class FakeDownloader:
    def __init__(self, files: dict[str, bytes] | None = None, default: bytes = JPEG) -> None:
        self.files = files or {}
        self.default = default
        self.calls: list[str] = []
        self.fail: Exception | None = None

    def __call__(self, url: str) -> tuple[bytes, str]:
        from infographic_store import detect_image_type

        self.calls.append(url)
        if self.fail:
            raise self.fail
        key = url.split("?", 1)[0].rsplit("/", 2)[-2]
        data = self.files.get(key, self.default)
        content_type = detect_image_type(data)
        if content_type is None:
            raise ValueError("Datei ist kein erlaubtes Bildformat (JPEG, PNG, WebP).")
        return data, content_type
