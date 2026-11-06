"""Web-side handling of G47IX infographics.

Runs inside the web server process: it re-evaluates infographic matches
against the current calendar events, attaches matched infographics to the
``/api/events`` payload, serves the image files and implements the admin
actions. All persistence goes through :mod:`infographic_store`.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

import event_matcher
from infographic_store import (
    InfographicStore,
    infographic_settings,
    read_json_file,
    status_counts,
    utc_now_iso,
    valid_record_id,
)

LOGGER = logging.getLogger("scrapedduck-ical.infographics")
SpeciesLookup = Callable[[str], list[dict[str, Any]]]
CANDIDATE_FIELDS = ("event_id", "title", "start", "end", "is_manual", "score", "reasons")


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def record_text(item: dict[str, Any]) -> str:
    discord = item.get("discord") or {}
    ocr = item.get("ocr") or {}
    return f"{discord.get('content') or ''}\n{ocr.get('text') or ''}"


class InfographicManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: dict[str, Any] = {"key": None, "mtime": None, "by_event": {}}

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def settings(config: dict[str, Any]) -> dict[str, Any]:
        return infographic_settings(config)

    def store(self, config: dict[str, Any]) -> InfographicStore:
        return InfographicStore.from_settings(self.settings(config))

    def invalidate(self) -> None:
        with self._lock:
            self._cache["key"] = None

    @staticmethod
    def public_item(item: dict[str, Any], attribution: dict[str, Any]) -> dict[str, Any]:
        discord = item.get("discord") or {}
        attachment = item.get("attachment") or {}
        return {
            "id": item["id"],
            "url": f"/infographic/{item['id']}",
            "width": attachment.get("width"),
            "height": attachment.get("height"),
            "postedAt": discord.get("posted_at"),
            "messageUrl": discord.get("message_url"),
            "creator": (item.get("attribution") or {}).get("creator") or attribution.get("creator"),
        }

    # -- matching ----------------------------------------------------------
    def _should_rematch(self, item: dict[str, Any], event_ids: set[str]) -> bool:
        matching = item.get("matching") or {}
        status = matching.get("status") or "pending"
        if matching.get("locked") or status in ("rejected", "error"):
            return False
        if not (item.get("attachment") or {}).get("local_path"):
            return False
        if matching.get("needs_rematch"):
            return True
        if status == "matched":
            # Keep automatic matches stable. A matched event that dropped out
            # of the calendar window (past event) keeps its infographic.
            return False
        return True

    def _apply_result(self, item: dict[str, Any], result: dict[str, Any]) -> bool:
        matching = item.setdefault("matching", {})
        new_values = {
            "status": result["status"],
            "event_id": result["event_id"],
            "event_title": result["event_title"],
            "score": int(result["score"]),
            "reasons": list(result["reasons"])[:8],
            "candidates": [{key: c.get(key) for key in CANDIDATE_FIELDS} for c in result["candidates"]],
            "method": "auto" if result["status"] == "matched" else None,
            "features": result.get("features"),
        }
        changed = any(matching.get(key) != value for key, value in new_values.items()) or matching.get("needs_rematch")
        if changed:
            matching.update(new_values)
            matching.pop("needs_rematch", None)
            matching["updated_at"] = utc_now_iso()
            item["updated_at"] = utc_now_iso()
            if result["status"] == "matched":
                LOGGER.info("Infografik %s automatisch zugeordnet: %s (%d Punkte)", item["id"], result["event_title"], result["score"])
        return bool(changed)

    def refresh(
        self,
        config: dict[str, Any],
        web_events: list[dict[str, Any]],
        events_key: str | None,
        species_lookup: SpeciesLookup,
    ) -> dict[str, list[dict[str, Any]]]:
        """Re-evaluate open infographics and return matched ones per event id."""
        settings = self.settings(config)
        if not settings["enabled"]:
            return {}
        store = InfographicStore.from_settings(settings)
        with self._lock:
            mtime = store.mtime()
            if self._cache["key"] == events_key and self._cache["mtime"] == mtime and events_key is not None:
                return self._cache["by_event"]

            local_tz = ZoneInfo(str(config.get("timezone") or "Europe/Berlin"))
            event_ids = {str(event.get("id")) for event in web_events}
            if mtime:
                with store.transaction() as payload:
                    for item in payload["items"]:
                        if not self._should_rematch(item, event_ids):
                            continue
                        posted = parse_timestamp((item.get("discord") or {}).get("posted_at")) or datetime.now(timezone.utc)
                        try:
                            result = event_matcher.match_infographic(
                                record_text(item), posted, web_events, settings, species_lookup, local_tz
                            )
                        except Exception:
                            LOGGER.exception("Matching für Infografik %s fehlgeschlagen", item.get("id"))
                            continue
                        self._apply_result(item, result)
                items = payload["items"]
            else:
                items = []

            by_event: dict[str, list[dict[str, Any]]] = {}
            for item in items:
                matching = item.get("matching") or {}
                if matching.get("status") != "matched" or not matching.get("event_id"):
                    continue
                path = store.resolve_file((item.get("attachment") or {}).get("local_path") or "")
                if path is None or not path.exists():
                    continue
                by_event.setdefault(str(matching["event_id"]), []).append(self.public_item(item, settings["attribution"]))
            for values in by_event.values():
                values.sort(key=lambda value: str(value.get("postedAt") or ""))
            self._cache.update({"key": events_key, "mtime": store.mtime(), "by_event": by_event})
            return by_event

    @staticmethod
    def augment(web_events: list[dict[str, Any]], by_event: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
        if not by_event:
            return web_events
        result = []
        for event in web_events:
            images = by_event.get(str(event.get("id")))
            if images:
                event = dict(event)
                event["infographics"] = images
            result.append(event)
        return result

    # -- files ---------------------------------------------------------------
    def image_file(self, config: dict[str, Any], record_id: str, is_admin: bool) -> tuple[bytes, str] | None:
        settings = self.settings(config)
        if not settings["enabled"] or not valid_record_id(record_id):
            return None
        store = InfographicStore.from_settings(settings)
        item = store.get(record_id)
        if not item:
            return None
        if (item.get("matching") or {}).get("status") != "matched" and not is_admin:
            return None
        attachment = item.get("attachment") or {}
        path = store.resolve_file(attachment.get("local_path") or "")
        content_type = str(attachment.get("content_type") or "")
        if path is None or content_type not in ("image/jpeg", "image/png", "image/webp"):
            return None
        try:
            return path.read_bytes(), content_type
        except OSError:
            return None

    # -- admin ---------------------------------------------------------------
    def admin_payload(self, config: dict[str, Any], web_events: list[dict[str, Any]]) -> dict[str, Any]:
        settings = self.settings(config)
        store = InfographicStore.from_settings(settings)
        payload = store.read() if settings["enabled"] else {"items": [], "skipped": {}, "deleted": []}
        titles = {str(event.get("id")): event.get("title") for event in web_events}
        items = []
        for item in sorted(payload["items"], key=lambda i: str((i.get("discord") or {}).get("posted_at") or ""), reverse=True):
            discord = item.get("discord") or {}
            attachment = item.get("attachment") or {}
            matching = item.get("matching") or {}
            ocr = item.get("ocr") or {}
            file_path = store.resolve_file(attachment.get("local_path") or "")
            items.append({
                "id": item["id"],
                "imageUrl": f"/infographic/{item['id']}" if attachment.get("local_path") else None,
                "fileExists": bool(file_path and file_path.exists()),
                "originalFilename": attachment.get("original_filename"),
                "contentType": attachment.get("content_type"),
                "size": attachment.get("size"),
                "width": attachment.get("width"),
                "height": attachment.get("height"),
                "downloadError": attachment.get("error"),
                "postedAt": discord.get("posted_at"),
                "messageUrl": discord.get("message_url"),
                "authorName": discord.get("author_name"),
                "content": discord.get("content"),
                "ocrStatus": ocr.get("status"),
                "ocrText": (ocr.get("text") or "")[:1500],
                "status": matching.get("status") or "pending",
                "score": matching.get("score") or 0,
                "reasons": matching.get("reasons") or [],
                "features": matching.get("features") or {},
                "eventId": matching.get("event_id"),
                "eventTitle": titles.get(str(matching.get("event_id"))) or matching.get("event_title"),
                "method": matching.get("method"),
                "locked": bool(matching.get("locked")),
                "candidates": matching.get("candidates") or [],
            })
        events = [
            {
                "id": event.get("id"),
                "title": event.get("title"),
                "start": event.get("start"),
                "end": event.get("end"),
                "allDay": event.get("allDay"),
                "isManual": event.get("isManual"),
                "category": event.get("category"),
            }
            for event in sorted(web_events, key=lambda e: str(e.get("start") or ""))
        ]
        return {
            "enabled": settings["enabled"],
            "items": items,
            "events": events,
            "summary": status_counts(payload["items"]),
            "skippedCount": len(payload.get("skipped") or {}),
            "thresholds": {
                "auto": settings["matching"]["auto_match_threshold"],
                "review": settings["matching"]["manual_review_threshold"],
            },
            "worker": self.worker_state(config),
        }

    def worker_state(self, config: dict[str, Any]) -> dict[str, Any]:
        settings = self.settings(config)
        state = read_json_file(settings["worker_state_file"])
        return {
            "status": state.get("status") or "unknown",
            "lastPollAt": state.get("last_poll_at"),
            "lastSuccessAt": state.get("last_success_at"),
            "lastError": state.get("last_error"),
            "updatedAt": state.get("updated_at"),
            "version": state.get("version"),
            "channels": len(state.get("channels") or {}),
        }

    def _update(self, config: dict[str, Any], record_id: str, change: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        if not valid_record_id(record_id):
            raise KeyError(record_id)
        store = self.store(config)
        found: dict[str, Any] | None = None
        with store.transaction() as payload:
            for item in payload["items"]:
                if item["id"] == record_id:
                    change(item)
                    item["updated_at"] = utc_now_iso()
                    found = item
                    break
        if found is None:
            raise KeyError(record_id)
        self.invalidate()
        return found

    def assign(self, config: dict[str, Any], record_id: str, event_id: str, event_title: str) -> dict[str, Any]:
        def change(item: dict[str, Any]) -> None:
            matching = item.setdefault("matching", {})
            matching.update({
                "status": "matched",
                "event_id": event_id,
                "event_title": event_title,
                "method": "manual",
                "locked": True,
                "updated_at": utc_now_iso(),
            })
            matching.pop("needs_rematch", None)
        return self._update(config, record_id, change)

    def unassign(self, config: dict[str, Any], record_id: str) -> dict[str, Any]:
        def change(item: dict[str, Any]) -> None:
            matching = item.setdefault("matching", {})
            matching.update({
                "status": "manual_review",
                "event_id": None,
                "event_title": None,
                "method": "manual",
                "locked": True,
                "updated_at": utc_now_iso(),
            })
        return self._update(config, record_id, change)

    def reject(self, config: dict[str, Any], record_id: str) -> dict[str, Any]:
        def change(item: dict[str, Any]) -> None:
            matching = item.setdefault("matching", {})
            matching.update({
                "status": "rejected",
                "event_id": None,
                "event_title": None,
                "method": "manual",
                "locked": True,
                "updated_at": utc_now_iso(),
            })
        return self._update(config, record_id, change)

    def rematch(self, config: dict[str, Any], record_id: str) -> dict[str, Any]:
        def change(item: dict[str, Any]) -> None:
            matching = item.setdefault("matching", {})
            matching.update({"status": "pending", "locked": False, "method": None, "needs_rematch": True})
        return self._update(config, record_id, change)

    def delete(self, config: dict[str, Any], record_ids: list[str]) -> int:
        store = self.store(config)
        removed = 0
        paths: list[str] = []
        with store.transaction() as payload:
            keep = []
            for item in payload["items"]:
                if item["id"] in record_ids:
                    paths.append((item.get("attachment") or {}).get("local_path") or "")
                    payload.setdefault("deleted", []).append(item["id"])
                    removed += 1
                else:
                    keep.append(item)
            payload["items"] = keep
        for path in paths:
            if path:
                store.delete_file(path)
        self.invalidate()
        return removed

    def cleanup(self, config: dict[str, Any], mode: str, older_than_days: int = 120) -> int:
        store = self.store(config)
        cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, older_than_days))
        targets = []
        for item in store.items():
            status = (item.get("matching") or {}).get("status")
            posted = parse_timestamp((item.get("discord") or {}).get("posted_at"))
            if mode == "rejected" and status == "rejected":
                targets.append(item["id"])
            elif mode == "older" and posted and posted < cutoff:
                targets.append(item["id"])
        return self.delete(config, targets) if targets else 0

    def health(self, config: dict[str, Any]) -> dict[str, Any]:
        settings = self.settings(config)
        if not settings["enabled"]:
            return {"enabled": False}
        store = InfographicStore.from_settings(settings)
        try:
            items = store.items()
        except Exception as exc:
            return {"enabled": True, "error": str(exc)}
        return {
            "enabled": True,
            "total": len(items),
            "by_status": status_counts(items),
            "files": store.file_count(),
            "worker": self.worker_state(config),
        }
