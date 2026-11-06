#!/usr/bin/env python3
"""Discord collector for G47IX infographics.

Runs as its own container (same image, different command). It polls the
configured Discord channel(s) through the REST API with a bot token, downloads
new image attachments, stores them in the shared ``/data`` volume, runs OCR
and records the metadata in ``infographic-events.json``. Matching against
calendar events happens in the web process, which knows the current events.

The bot never sends, edits or deletes anything. It only needs the channel
permissions "View Channel" and "Read Message History" plus the privileged
Message Content intent (without it Discord returns empty attachments).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import signal
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from infographic_store import (
    CONTENT_TYPES_BY_EXTENSION,
    IMAGE_EXTENSIONS_BY_TYPE,
    InfographicStore,
    detect_image_type,
    image_dimensions,
    infographic_settings,
    make_record_id,
    read_json_file,
    sha256_hex,
    utc_now_iso,
    write_json_file_atomic,
)

LOGGER = logging.getLogger("scrapedduck-ical.discord")
API_BASE = "https://discord.com/api/v10"
WORKER_VERSION = "1.7.0"
USER_AGENT = f"DiscordBot (https://github.com/EmreWest/pogo-kalender, {WORKER_VERSION})"
SNOWFLAKE = re.compile(r"[0-9]{5,25}")


class DiscordAPIError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"Discord-API {status}: {message}")
        self.status = status


class DiscordConfigError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

def _snowflake_list(value: str, name: str) -> list[str]:
    items = [part.strip() for part in str(value or "").replace(";", ",").split(",") if part.strip()]
    for item in items:
        if not SNOWFLAKE.fullmatch(item):
            raise DiscordConfigError(f"{name} enthält einen ungültigen Wert (nur Discord-IDs aus Ziffern).")
    return list(dict.fromkeys(items))


def discord_environment(environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Read and validate the Discord settings from environment variables."""
    env = environ if environ is not None else os.environ
    token = str(env.get("DISCORD_BOT_TOKEN") or "").strip()
    guild_ids = _snowflake_list(env.get("DISCORD_GUILD_ID", ""), "DISCORD_GUILD_ID")
    channel_ids = _snowflake_list(env.get("DISCORD_CHANNEL_IDS", ""), "DISCORD_CHANNEL_IDS")
    authors = _snowflake_list(env.get("DISCORD_ALLOWED_AUTHOR_IDS", ""), "DISCORD_ALLOWED_AUTHOR_IDS")
    missing = []
    if not token:
        missing.append("DISCORD_BOT_TOKEN")
    if len(guild_ids) != 1:
        missing.append("DISCORD_GUILD_ID")
    if not channel_ids:
        missing.append("DISCORD_CHANNEL_IDS")
    if missing:
        raise DiscordConfigError("Fehlende Discord-Konfiguration: " + ", ".join(missing))
    return {
        "token": token,
        "guild_id": guild_ids[0],
        "channel_ids": channel_ids,
        "allowed_author_ids": set(authors),
    }


class TokenFilter(logging.Filter):
    """Defensive filter: never let the bot token reach a log line."""

    def __init__(self, token: str) -> None:
        super().__init__()
        self.token = token

    def filter(self, record: logging.LogRecord) -> bool:
        if self.token and len(self.token) > 8:
            message = record.getMessage()
            if self.token in message:
                record.msg = message.replace(self.token, "***")
                record.args = ()
        return True


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def host_allowed(url: str, allowed_hosts: list[str] | set[str]) -> bool:
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").casefold()
    return parsed.scheme == "https" and bool(host) and host in {h.casefold() for h in allowed_hosts} and parsed.port in (None, 443)


class AllowlistRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow redirects only to HTTPS hosts on the allowlist (SSRF protection)."""

    def __init__(self, allowed_hosts: list[str]) -> None:
        super().__init__()
        self.allowed_hosts = allowed_hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        if not host_allowed(newurl, self.allowed_hosts):
            raise urllib.error.HTTPError(newurl, code, "Weiterleitung auf nicht erlaubten Host blockiert", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download_image(
    url: str,
    allowed_hosts: list[str],
    max_bytes: int,
    timeout: int,
    opener: urllib.request.OpenerDirector | None = None,
) -> tuple[bytes, str]:
    """Download an image from an allowed host and verify size and format."""
    if not host_allowed(url, allowed_hosts):
        raise ValueError("Download-Host ist nicht erlaubt.")
    opener = opener or urllib.request.build_opener(AllowlistRedirectHandler(allowed_hosts))
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "image/*"})
    try:
        with opener.open(request, timeout=timeout) as response:
            length = response.headers.get("Content-Length")
            if length and length.isdigit() and int(length) > max_bytes:
                raise ValueError("Datei ist größer als das konfigurierte Limit.")
            data = response.read(max_bytes + 1)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ConnectionError(f"Download fehlgeschlagen: {exc}") from exc
    if len(data) > max_bytes:
        raise ValueError("Datei ist größer als das konfigurierte Limit.")
    content_type = detect_image_type(data)
    if content_type is None:
        raise ValueError("Datei ist kein erlaubtes Bildformat (JPEG, PNG, WebP).")
    return data, content_type


class DiscordClient:
    """Minimal read-only Discord REST client with rate-limit handling."""

    def __init__(
        self,
        token: str,
        transport: Callable[[str, dict[str, str], int], tuple[int, dict[str, str], bytes]] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        timeout: int = 20,
    ) -> None:
        self._token = token
        self._transport = transport or self._urllib_transport
        self._sleep = sleep
        self._timeout = timeout

    @staticmethod
    def _urllib_transport(url: str, headers: dict[str, str], timeout: int) -> tuple[int, dict[str, str], bytes]:
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, {k.casefold(): v for k, v in response.headers.items()}, response.read()
        except urllib.error.HTTPError as exc:
            body = exc.read() if exc.fp else b""
            return exc.code, {k.casefold(): v for k, v in (exc.headers or {}).items()}, body

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
        url = f"{API_BASE}{path}" + (f"?{query}" if query else "")
        headers = {"Authorization": f"Bot {self._token}", "User-Agent": USER_AGENT, "Accept": "application/json"}
        attempts = 0
        while True:
            attempts += 1
            try:
                status, response_headers, body = self._transport(url, headers, self._timeout)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempts >= 3:
                    raise ConnectionError(f"Discord nicht erreichbar: {getattr(exc, 'reason', exc)}") from None
                self._sleep(min(30, 2 ** attempts))
                continue

            if status == 429:
                retry_after = 5.0
                try:
                    retry_after = float(json.loads(body.decode("utf-8")).get("retry_after", retry_after))
                except Exception:
                    try:
                        retry_after = float(response_headers.get("retry-after", retry_after))
                    except ValueError:
                        pass
                if attempts >= 5:
                    raise DiscordAPIError(429, "Rate-Limit wiederholt erreicht")
                LOGGER.warning("Discord-Rate-Limit erreicht, warte %.1f s", retry_after)
                self._sleep(min(max(retry_after, 0.5), 120))
                continue

            if status >= 500:
                if attempts >= 3:
                    raise DiscordAPIError(status, "Serverfehler bei Discord")
                self._sleep(min(30, 2 ** attempts))
                continue

            if status >= 400:
                message = ""
                try:
                    message = str(json.loads(body.decode("utf-8")).get("message") or "")
                except Exception:
                    pass
                hints = {
                    401: "Token ungültig oder zurückgesetzt",
                    403: "Keine Berechtigung (View Channel / Read Message History prüfen)",
                    404: "Kanal nicht gefunden oder Bot nicht im Server",
                }
                raise DiscordAPIError(status, message or hints.get(status, "Anfrage abgelehnt"))

            remaining = response_headers.get("x-ratelimit-remaining")
            reset_after = response_headers.get("x-ratelimit-reset-after")
            if remaining == "0" and reset_after:
                try:
                    self._sleep(min(float(reset_after), 60))
                except ValueError:
                    pass
            try:
                return json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise DiscordAPIError(status, "Ungültige JSON-Antwort") from exc

    def get_channel(self, channel_id: str) -> dict[str, Any]:
        data = self.get(f"/channels/{channel_id}")
        return data if isinstance(data, dict) else {}

    def get_messages(
        self, channel_id: str, limit: int = 50, after: str | None = None, before: str | None = None
    ) -> list[dict[str, Any]]:
        data = self.get(
            f"/channels/{channel_id}/messages",
            {"limit": max(1, min(int(limit), 100)), "after": after, "before": before},
        )
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict) and SNOWFLAKE.fullmatch(str(item.get("id") or ""))]


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------

def snowflake_int(value: Any) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


def message_text(message: dict[str, Any]) -> str:
    parts = [str(message.get("content") or "")]
    for embed in message.get("embeds") or []:
        if isinstance(embed, dict):
            for key in ("title", "description"):
                if embed.get(key):
                    parts.append(str(embed[key]))
            for field in embed.get("fields") or []:
                if isinstance(field, dict):
                    parts.append(f"{field.get('name') or ''} {field.get('value') or ''}")
    return "\n".join(part for part in parts if part.strip())[:4000]


def message_url(guild_id: str, channel_id: str, message: dict[str, Any]) -> tuple[str, str]:
    own = f"https://discord.com/channels/{guild_id}/{channel_id}/{message.get('id')}"
    reference = message.get("message_reference")
    if isinstance(reference, dict):
        ref_guild = str(reference.get("guild_id") or "")
        ref_channel = str(reference.get("channel_id") or "")
        ref_message = str(reference.get("message_id") or "")
        if all(SNOWFLAKE.fullmatch(v) for v in (ref_guild, ref_channel, ref_message)):
            # Crossposted from a followed announcement channel: link the original.
            return f"https://discord.com/channels/{ref_guild}/{ref_channel}/{ref_message}", own
    return own, own


def strip_query(url: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return ""
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


class InfographicCollector:
    def __init__(
        self,
        settings: dict[str, Any],
        store: InfographicStore,
        client: DiscordClient,
        guild_id: str,
        channel_ids: list[str],
        allowed_author_ids: set[str] | None = None,
        downloader: Callable[[str], tuple[bytes, str]] | None = None,
        ocr_func: Callable[[Path], str] | None = None,
        state_file: str | Path | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.client = client
        self.guild_id = guild_id
        self.channel_ids = channel_ids
        self.allowed_author_ids = set(allowed_author_ids or set())
        self.max_bytes = int(settings["max_file_size_mb"]) * 1024 * 1024
        self.downloader = downloader or (
            lambda url: download_image(
                url, settings["allowed_download_hosts"], self.max_bytes, settings["download_timeout_seconds"]
            )
        )
        self.ocr_func = ocr_func
        self.state_file = Path(state_file or settings["worker_state_file"])
        self.state = read_json_file(self.state_file)
        self.state.setdefault("channels", {})
        self.verified_channels: set[str] = set()

    # -- state -------------------------------------------------------------
    def save_state(self, **updates: Any) -> None:
        self.state.update(updates)
        self.state["version"] = WORKER_VERSION
        self.state["updated_at"] = utc_now_iso()
        try:
            write_json_file_atomic(self.state_file, self.state)
        except OSError as exc:
            LOGGER.warning("Worker-Status konnte nicht geschrieben werden: %s", exc)

    def channel_state(self, channel_id: str) -> dict[str, Any]:
        channels = self.state.setdefault("channels", {})
        return channels.setdefault(channel_id, {"last_message_id": None, "historical_done": False})

    # -- discord -----------------------------------------------------------
    def verify_channel(self, channel_id: str) -> None:
        if channel_id in self.verified_channels:
            return
        channel = self.client.get_channel(channel_id)
        guild = str(channel.get("guild_id") or "")
        if guild != self.guild_id:
            raise DiscordConfigError(
                f"Kanal {channel_id} gehört nicht zum konfigurierten Server (DISCORD_GUILD_ID)."
            )
        self.verified_channels.add(channel_id)
        self.channel_state(channel_id)["name"] = str(channel.get("name") or "")[:100]

    def author_allowed(self, message: dict[str, Any]) -> bool:
        if not self.allowed_author_ids:
            return True
        author = message.get("author") if isinstance(message.get("author"), dict) else {}
        candidates = {str(author.get("id") or ""), str(message.get("webhook_id") or "")}
        return bool(candidates & self.allowed_author_ids)

    def image_sources(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        message_id = str(message["id"])
        sources: list[dict[str, Any]] = []
        for attachment in message.get("attachments") or []:
            if not isinstance(attachment, dict):
                continue
            attachment_id = str(attachment.get("id") or "")
            if not SNOWFLAKE.fullmatch(attachment_id):
                continue
            sources.append({
                "record_id": make_record_id(message_id, attachment_id),
                "kind": "attachment",
                "attachment_id": attachment_id,
                "url": str(attachment.get("url") or ""),
                "filename": str(attachment.get("filename") or "")[:200],
                "content_type": str(attachment.get("content_type") or ""),
                "size": snowflake_int(attachment.get("size")),
                "width": attachment.get("width"),
                "height": attachment.get("height"),
            })
        if self.settings.get("include_embed_images", True):
            for embed in message.get("embeds") or []:
                if not isinstance(embed, dict):
                    continue
                embed_type = str(embed.get("type") or "")
                candidates = []
                if isinstance(embed.get("image"), dict):
                    candidates.append(embed["image"])
                if embed_type == "image" and isinstance(embed.get("thumbnail"), dict):
                    candidates.append(embed["thumbnail"])
                for image in candidates:
                    url = ""
                    for key in ("url", "proxy_url"):
                        value = str(image.get(key) or "")
                        if host_allowed(value, self.settings["allowed_download_hosts"]):
                            url = value
                            break
                    if not url:
                        continue
                    digest = hashlib.sha1(strip_query(url).encode("utf-8")).hexdigest()[:12]
                    sources.append({
                        "record_id": make_record_id(message_id, "e" + digest),
                        "kind": "embed",
                        "attachment_id": None,
                        "url": url,
                        "filename": Path(urllib.parse.urlsplit(url).path).name[:200],
                        "content_type": "",
                        "size": 0,
                        "width": image.get("width"),
                        "height": image.get("height"),
                    })
        unique: dict[str, dict[str, Any]] = {}
        for source in sources:
            unique.setdefault(source["record_id"], source)
        return list(unique.values())

    def precheck(self, source: dict[str, Any]) -> str | None:
        """Return a reason when the source must be skipped before downloading."""
        extension = Path(source["filename"]).suffix.casefold()
        if extension and extension not in self.settings["allowed_extensions"]:
            return f"Dateityp {extension} nicht erlaubt"
        if not extension and source["kind"] == "attachment":
            return "Dateiname ohne Endung"
        declared = source.get("content_type") or ""
        if declared and declared.split(";", 1)[0].strip() not in IMAGE_EXTENSIONS_BY_TYPE:
            return f"MIME-Typ {declared} nicht erlaubt"
        if source.get("size") and int(source["size"]) > self.max_bytes:
            return "Datei zu groß"
        if not host_allowed(source["url"], self.settings["allowed_download_hosts"]):
            return "Download-Host nicht erlaubt"
        return None

    def build_record(
        self,
        channel_id: str,
        message: dict[str, Any],
        source: dict[str, Any],
    ) -> dict[str, Any]:
        author = message.get("author") if isinstance(message.get("author"), dict) else {}
        posted_at = str(message.get("timestamp") or utc_now_iso())
        link, own_link = message_url(self.guild_id, channel_id, message)
        return {
            "id": source["record_id"],
            "discord": {
                "guild_id": self.guild_id,
                "channel_id": channel_id,
                "message_id": str(message["id"]),
                "author_id": str(author.get("id") or ""),
                "author_name": str(author.get("username") or "")[:100],
                "webhook_id": str(message.get("webhook_id") or "") or None,
                "message_url": link,
                "local_message_url": own_link,
                "posted_at": posted_at,
                "edited_at": message.get("edited_timestamp"),
                "content": message_text(message),
            },
            "attachment": {
                "kind": source["kind"],
                "attachment_id": source["attachment_id"],
                "original_filename": source["filename"],
                "content_type": None,
                "original_url": strip_query(source["url"]),
                "local_path": None,
                "sha256": None,
                "size": None,
                "width": None,
                "height": None,
                "download_attempts": 0,
                "error": None,
            },
            "ocr": {"status": "pending", "text": "", "updated_at": None, "error": None},
            "matching": {
                "event_id": None,
                "event_title": None,
                "status": "pending",
                "score": 0,
                "reasons": [],
                "candidates": [],
                "method": None,
                "locked": False,
                "updated_at": None,
            },
            "attribution": dict(self.settings["attribution"]),
            "created_at": utc_now_iso(),
            "updated_at": utc_now_iso(),
        }

    def fetch_and_store(self, record: dict[str, Any], url: str) -> None:
        """Download the image, write it to disk and fill file metadata (no lock held)."""
        attachment = record["attachment"]
        attachment["download_attempts"] = int(attachment.get("download_attempts") or 0) + 1
        data, content_type = self.downloader(url)
        if len(data) > self.max_bytes:
            raise ValueError("Datei ist größer als das konfigurierte Limit.")
        extension = IMAGE_EXTENSIONS_BY_TYPE.get(content_type)
        allowed_types = {CONTENT_TYPES_BY_EXTENSION[ext] for ext in self.settings["allowed_extensions"]}
        if not extension or content_type not in allowed_types:
            raise ValueError("Bildformat ist nicht erlaubt.")
        posted = record["discord"]["posted_at"]
        try:
            stamp = datetime.fromisoformat(posted.replace("Z", "+00:00"))
        except ValueError:
            stamp = datetime.now(timezone.utc)
        relative = f"{stamp:%Y}/{stamp:%m}/{record['id']}{extension}"
        self.store.write_image(relative, data)
        dimensions = image_dimensions(data, content_type)
        attachment.update({
            "content_type": content_type,
            "local_path": relative,
            "sha256": sha256_hex(data),
            "size": len(data),
            "width": dimensions[0] if dimensions else None,
            "height": dimensions[1] if dimensions else None,
            "error": None,
        })

    def run_ocr_for(self, record: dict[str, Any]) -> None:
        if self.ocr_func is None:  # OCR disabled or Tesseract missing
            record["ocr"].update({"status": "skipped", "updated_at": utc_now_iso()})
            return
        path = self.store.resolve_file(record["attachment"].get("local_path") or "")
        if path is None or not path.exists():
            record["ocr"].update({"status": "error", "error": "Datei fehlt", "updated_at": utc_now_iso()})
            return
        try:
            text = self.ocr_func(path)
            record["ocr"].update({"status": "done", "text": text, "error": None, "updated_at": utc_now_iso()})
        except Exception as exc:
            LOGGER.warning("OCR für %s fehlgeschlagen: %s", record["id"], exc)
            record["ocr"].update({"status": "error", "error": str(exc)[:300], "updated_at": utc_now_iso()})

    def process_message(self, channel_id: str, message: dict[str, Any]) -> list[str]:
        """Handle one Discord message. Returns ids of newly stored infographics."""
        if not self.author_allowed(message):
            return []
        sources = self.image_sources(message)
        if not sources:
            return []
        payload = self.store.read()
        existing = {item["id"]: item for item in payload["items"]}
        deleted = set(payload.get("deleted") or [])
        skipped = payload.get("skipped") or {}
        known_hashes = {
            str((item.get("attachment") or {}).get("sha256")): item["id"]
            for item in payload["items"]
            if (item.get("attachment") or {}).get("sha256")
        }

        self._apply_edit(message, [s["record_id"] for s in sources if s["record_id"] in existing])

        created: list[str] = []
        for source in sources:
            record_id = source["record_id"]
            if record_id in deleted or record_id in skipped:
                continue
            current = existing.get(record_id)
            if current is not None:
                if current["matching"].get("status") == "error" and int(current["attachment"].get("download_attempts") or 0) < 3:
                    self._retry_download(current, source)
                continue

            reason = self.precheck(source)
            if reason:
                LOGGER.info("Bild %s übersprungen: %s", record_id, reason)
                self._mark_skipped(record_id, reason)
                continue

            record = self.build_record(channel_id, message, source)
            try:
                self.fetch_and_store(record, source["url"])
            except ValueError as exc:
                LOGGER.info("Bild %s übersprungen: %s", record_id, exc)
                self._mark_skipped(record_id, str(exc))
                continue
            except Exception as exc:
                LOGGER.warning("Bild %s konnte nicht geladen werden: %s", record_id, exc)
                record["attachment"]["error"] = str(exc)[:300]
                record["matching"]["status"] = "error"
                self._insert(record)
                continue

            duplicate_of = known_hashes.get(record["attachment"]["sha256"])
            if duplicate_of:
                LOGGER.info("Bild %s ist identisch mit %s und wird nicht doppelt gespeichert", record_id, duplicate_of)
                self.store.delete_file(record["attachment"]["local_path"])
                self._mark_skipped(record_id, f"Duplikat von {duplicate_of}")
                continue

            self.run_ocr_for(record)
            if self._insert(record):
                known_hashes[record["attachment"]["sha256"]] = record_id
                created.append(record_id)
                LOGGER.info("Infografik %s gespeichert (%s)", record_id, record["attachment"]["original_filename"])
        return created

    def _retry_download(self, record: dict[str, Any], source: dict[str, Any]) -> None:
        updated = json.loads(json.dumps(record))
        try:
            self.fetch_and_store(updated, source["url"])
        except Exception as exc:
            updated["attachment"]["error"] = str(exc)[:300]
            with self.store.transaction() as payload:
                for item in payload["items"]:
                    if item["id"] == record["id"]:
                        item["attachment"] = updated["attachment"]
            return
        self.run_ocr_for(updated)
        updated["matching"]["status"] = "pending"
        updated["updated_at"] = utc_now_iso()
        with self.store.transaction() as payload:
            payload["items"] = [updated if item["id"] == record["id"] else item for item in payload["items"]]

    def _apply_edit(self, message: dict[str, Any], record_ids: list[str]) -> None:
        if not record_ids:
            return
        edited = message.get("edited_timestamp")
        text = message_text(message)
        with self.store.transaction() as payload:
            for item in payload["items"]:
                if item["id"] not in record_ids:
                    continue
                discord = item["discord"]
                if discord.get("edited_at") == edited and discord.get("content") == text:
                    continue
                discord["edited_at"] = edited
                discord["content"] = text
                item["updated_at"] = utc_now_iso()
                matching = item["matching"]
                if not matching.get("locked") and matching.get("status") in ("pending", "manual_review", "matched"):
                    # Re-evaluate automatic results with the edited text.
                    matching["status"] = "pending"
                    matching["needs_rematch"] = True
                LOGGER.info("Nachricht %s wurde bearbeitet, Infografik %s aktualisiert", message.get("id"), item["id"])

    def _mark_skipped(self, record_id: str, reason: str) -> None:
        with self.store.transaction() as payload:
            payload.setdefault("skipped", {})[record_id] = reason[:200]

    def _insert(self, record: dict[str, Any]) -> bool:
        with self.store.transaction() as payload:
            if any(item["id"] == record["id"] for item in payload["items"]):
                return False
            if record["id"] in set(payload.get("deleted") or []):
                self.store.delete_file(record["attachment"].get("local_path") or "")
                return False
            payload["items"].append(record)
            return True

    # -- polling -----------------------------------------------------------
    def historical_import(self, channel_id: str) -> None:
        historical = self.settings["historical_import"]
        state = self.channel_state(channel_id)
        limit = int(historical["message_limit"]) if historical["enabled"] else 0
        collected: list[dict[str, Any]] = []
        before: str | None = None
        while len(collected) < limit:
            batch = self.client.get_messages(channel_id, limit=min(100, limit - len(collected)), before=before)
            if not batch:
                break
            collected.extend(batch)
            before = min((str(m["id"]) for m in batch), key=snowflake_int)
            if len(batch) < 100:
                break
        if limit == 0:
            newest = self.client.get_messages(channel_id, limit=1)
            collected = []
            if newest:
                state["last_message_id"] = str(newest[0]["id"])
        LOGGER.info("Historischer Import für Kanal %s: %d Nachrichten", channel_id, len(collected))
        for message in sorted(collected, key=lambda m: snowflake_int(m["id"])):
            self.process_message(channel_id, message)
            self._advance_cursor(channel_id, str(message["id"]))
        state["historical_done"] = True
        self.save_state()

    def _advance_cursor(self, channel_id: str, message_id: str) -> None:
        state = self.channel_state(channel_id)
        if snowflake_int(message_id) > snowflake_int(state.get("last_message_id")):
            state["last_message_id"] = message_id

    def poll_channel(self, channel_id: str) -> int:
        self.verify_channel(channel_id)
        state = self.channel_state(channel_id)
        if not state.get("historical_done"):
            self.historical_import(channel_id)
            return 0
        processed = 0
        if state.get("last_message_id"):
            for _page in range(20):
                batch = self.client.get_messages(channel_id, limit=100, after=state["last_message_id"])
                if not batch:
                    break
                for message in sorted(batch, key=lambda m: snowflake_int(m["id"])):
                    self.process_message(channel_id, message)
                    self._advance_cursor(channel_id, str(message["id"]))
                    processed += 1
                self.save_state()
                if len(batch) < 100:
                    break
        else:
            newest = self.client.get_messages(channel_id, limit=50)
            for message in sorted(newest, key=lambda m: snowflake_int(m["id"])):
                self.process_message(channel_id, message)
                self._advance_cursor(channel_id, str(message["id"]))
                processed += 1

        recheck = int(self.settings.get("recheck_recent_messages") or 0)
        if recheck:
            known_messages = {
                str(item["discord"].get("message_id")) for item in self.store.items()
                if str(item["discord"].get("channel_id")) == channel_id
            }
            for message in self.client.get_messages(channel_id, limit=recheck):
                if str(message["id"]) in known_messages:
                    self.process_message(channel_id, message)
        return processed

    def poll_once(self) -> dict[str, Any]:
        started = utc_now_iso()
        errors: list[str] = []
        processed = 0
        for channel_id in self.channel_ids:
            try:
                processed += self.poll_channel(channel_id)
            except (DiscordAPIError, DiscordConfigError, ConnectionError) as exc:
                LOGGER.error("Kanal %s: %s", channel_id, exc)
                errors.append(f"Kanal {channel_id}: {exc}")
        status = "ok" if not errors else ("error" if len(errors) == len(self.channel_ids) else "degraded")
        result = {
            "status": status,
            "last_poll_at": started,
            "last_error": "; ".join(errors) or None,
            "last_processed": processed,
        }
        if not errors:
            result["last_success_at"] = utc_now_iso()
        self.save_state(**result)
        return result


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def healthcheck(config: dict[str, Any]) -> int:
    settings = infographic_settings(config)
    state = read_json_file(settings["worker_state_file"])
    status = state.get("status")
    if status in ("disabled", "unconfigured"):
        return 0
    stamp = state.get("updated_at")
    if not stamp:
        return 1
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(stamp)).total_seconds()
    except ValueError:
        return 1
    return 0 if age < settings["poll_interval_seconds"] * 3 + 600 else 1


def show_authors(env: dict[str, Any]) -> int:
    """Print who posted the latest messages – helps to find the webhook ID."""
    client = DiscordClient(env["token"])
    for channel_id in env["channel_ids"]:
        try:
            channel = client.get_channel(channel_id)
            messages = client.get_messages(channel_id, limit=20)
        except (DiscordAPIError, ConnectionError) as exc:
            print(f"Kanal {channel_id}: {exc}")
            continue
        print(f"Kanal {channel_id} (#{channel.get('name', '?')}, Server {channel.get('guild_id')})")
        seen: set[tuple[str, str]] = set()
        for message in messages:
            author = message.get("author") if isinstance(message.get("author"), dict) else {}
            key = (str(author.get("id") or ""), str(message.get("webhook_id") or ""))
            if key in seen:
                continue
            seen.add(key)
            images = len(message.get("attachments") or [])
            kind = "Webhook/gefolgter Kanal" if message.get("webhook_id") else "Benutzer/Bot"
            print(f"  Autor-ID {key[0]}  Name: {author.get('username', '?')}  ({kind}, {images} Anhänge in der neuesten Nachricht)")
        if not messages:
            print("  Keine Nachrichten gefunden.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Discord-Collector für G47IX-Infografiken")
    parser.add_argument("--once", action="store_true", help="nur einen Abrufzyklus ausführen")
    parser.add_argument("--healthcheck", action="store_true", help="Status für Docker-Healthcheck prüfen")
    parser.add_argument(
        "--show-authors",
        action="store_true",
        help="Autoren/Webhook-IDs der letzten Nachrichten im Kanal anzeigen (für DISCORD_ALLOWED_AUTHOR_IDS)",
    )
    args = parser.parse_args(argv)

    from app import load_config  # reuse the web app's configuration loader
    from ocr import run_ocr, tesseract_available

    config = load_config()
    if args.healthcheck:
        return healthcheck(config)

    settings = infographic_settings(config)
    store = InfographicStore.from_settings(settings)
    state_path = Path(settings["worker_state_file"])

    def write_state(status: str, message: str) -> None:
        state = read_json_file(state_path)
        state.update({"status": status, "last_error": message, "updated_at": utc_now_iso(), "version": WORKER_VERSION})
        write_json_file_atomic(state_path, state)

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    if not settings["enabled"] and not args.show_authors:
        LOGGER.info("Infografiken sind in der config.json deaktiviert; Worker bleibt im Leerlauf.")
        write_state("disabled", None)  # type: ignore[arg-type]
        while not stop.wait(3600):
            write_state("disabled", None)  # type: ignore[arg-type]
        return 0

    try:
        env = discord_environment()
    except DiscordConfigError as exc:
        if args.show_authors or args.once:
            LOGGER.error("%s", exc)
            return 1
        LOGGER.warning("%s – Worker bleibt im Leerlauf. Kalender und Website laufen normal weiter.", exc)
        write_state("unconfigured", str(exc))
        while not stop.wait(3600):
            write_state("unconfigured", str(exc))
        return 0

    if args.show_authors:
        return show_authors(env)

    root_logger = logging.getLogger()
    token_filter = TokenFilter(env["token"])
    for handler in root_logger.handlers:
        handler.addFilter(token_filter)

    ocr_settings = settings["ocr"]
    ocr_func = None
    if ocr_settings["enabled"]:
        if tesseract_available():
            ocr_func = lambda path: run_ocr(  # noqa: E731
                path, ocr_settings["languages"], ocr_settings["timeout_seconds"], ocr_settings["max_chars"]
            )
        else:
            LOGGER.warning("Tesseract nicht gefunden – OCR wird übersprungen.")

    collector = InfographicCollector(
        settings,
        store,
        DiscordClient(env["token"]),
        env["guild_id"],
        env["channel_ids"],
        env["allowed_author_ids"],
        ocr_func=ocr_func,
        state_file=state_path,
    )
    LOGGER.info(
        "Discord-Worker gestartet: %d Kanal/Kanäle, Abruf alle %d s, erlaubte Autoren: %s",
        len(env["channel_ids"]),
        settings["poll_interval_seconds"],
        len(env["allowed_author_ids"]) or "alle",
    )

    failures = 0
    while not stop.is_set():
        try:
            result = collector.poll_once()
            failures = failures + 1 if result["status"] == "error" else 0
        except Exception as exc:  # never crash the loop
            failures += 1
            LOGGER.exception("Abrufzyklus fehlgeschlagen")
            collector.save_state(status="error", last_error=str(exc)[:500], last_poll_at=utc_now_iso())
        if args.once:
            break
        delay = settings["poll_interval_seconds"] * (2 ** min(failures, 3) if failures else 1)
        stop.wait(min(delay, 3600))
    LOGGER.info("Discord-Worker beendet.")
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    sys.exit(main())
