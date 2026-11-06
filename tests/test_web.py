"""Web server: existing calendar features plus infographic display and admin."""

from __future__ import annotations

import json
import shutil
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

from tests import helpers
from tests.helpers import CHANNEL_ID, GUILD_ID, WEBHOOK_ID, FakeDiscordClient, FakeDownloader, attachment, message

import app
from discord_worker import InfographicCollector
from infographic_store import InfographicStore, infographic_settings

SOURCE_EVENTS = [
    {
        "eventID": "giratina-altered-forme-in-5-star-raid-battles-november-2026",
        "name": "Giratina (Altered Forme) in 5-star Raid Battles",
        "eventType": "raid-battles",
        "heading": "Raid Battles",
        "link": "https://leekduck.com/events/giratina/",
        "start": "2026-11-04T06:00:00.000",
        "end": "2026-11-08T22:00:00.000",
        "extraData": {"raidbattles": {"bosses": [{"name": "Giratina (Altered Forme)", "image": ""}]}},
    },
    {
        "eventID": "shadow-tornadus-incarnate-forme-in-shadow-raids-november-4-2026",
        "name": "Shadow Tornadus (Incarnate Forme) in Shadow Raids",
        "eventType": "raid-battles",
        "heading": "Raid Battles",
        "link": "https://leekduck.com/events/tornadus/",
        "start": "2026-11-04T06:00:00.000",
        "end": "2026-11-08T22:00:00.000",
    },
    {
        "eventID": "twilight-wishes-2026",
        "name": "Twilight Wishes",
        "eventType": "event",
        "heading": "Event",
        "link": "https://leekduck.com/events/twilight-wishes-2026/",
        "start": "2026-11-24T10:00:00.000",
        "end": "2026-11-30T20:00:00.000",
    },
]


class Client:
    def __init__(self, base: str) -> None:
        self.base = base
        self.cookie: str | None = None
        self.csrf: str | None = None

    def request(self, method: str, path: str, body: Any = None, csrf: bool = True) -> tuple[int, dict[str, str], bytes]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        if self.cookie:
            request.add_header("Cookie", self.cookie)
        if csrf and self.csrf and method != "GET":
            request.add_header("X-CSRF-Token", self.csrf)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def json(self, method: str, path: str, body: Any = None, csrf: bool = True) -> tuple[int, Any]:
        status, _headers, payload = self.request(method, path, body, csrf)
        return status, json.loads(payload.decode("utf-8")) if payload else None

    def login(self) -> None:
        status, headers, payload = self.request("POST", "/api/admin/login", {"password": helpers.TEST_PASSWORD})
        assert status == 200, payload
        self.cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.csrf = json.loads(payload)["csrf"]


class WebTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.data_dir = helpers.fresh_data_dir("web")
        cls.config = helpers.make_config(cls.data_dir)
        helpers.write_config(cls.config)
        cls._original_fetch = app.fetch_events
        app.fetch_events = lambda config: [dict(event) for event in SOURCE_EVENTS]
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), app.CalendarHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        app.fetch_events = cls._original_fetch

    def setUp(self) -> None:
        helpers.write_config(self.config)
        settings = infographic_settings(self.config)
        for path in (settings["metadata_file"], settings["worker_state_file"]):
            Path(path).unlink(missing_ok=True)
        shutil.rmtree(settings["storage_dir"], ignore_errors=True)
        Path(self.config["manual_events"]["file"]).unlink(missing_ok=True)
        app.invalidate_calendar_cache()
        app.INFOGRAPHICS.invalidate()
        with app.LOGIN_ATTEMPTS_LOCK:
            app.LOGIN_ATTEMPTS.clear()
        self.settings = settings
        self.store = InfographicStore.from_settings(settings)
        self.client = Client(self.base)

    def seed(self, message_id: str, content: str = "", ocr_text: str = "", posted: str = "2026-11-03T12:00:00+00:00") -> str:
        collector = InfographicCollector(
            self.settings,
            self.store,
            FakeDiscordClient(),
            GUILD_ID,
            [CHANNEL_ID],
            {WEBHOOK_ID},
            downloader=FakeDownloader(default=helpers.JPEG + message_id.encode()),
            ocr_func=lambda _path: ocr_text,
        )
        attachment_id = "14" + message_id[2:]
        created = collector.process_message(CHANNEL_ID, message(message_id, content, [attachment(attachment_id)], timestamp=posted))
        self.assertEqual(len(created), 1)
        return created[0]

    def api_events(self) -> dict[str, dict[str, Any]]:
        status, payload = self.client.json("GET", "/api/events")
        self.assertEqual(status, 200)
        return {event["id"]: event for event in payload["events"]}

    # --- existing features --------------------------------------------------
    def test_20_existing_calendar_and_ical_still_work(self) -> None:
        status, headers, body = self.client.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"modal-infographics", body)
        status, headers, ics = self.client.request("GET", "/pokemon-go-de.ics")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/calendar"))
        text = ics.decode("utf-8")
        self.assertIn("BEGIN:VCALENDAR", text)
        self.assertEqual(text.count("BEGIN:VEVENT"), 3)
        self.assertIn("SUMMARY:Giratina (Altered Forme) in Stufe-5-Raids", text)
        self.assertNotIn("infographic", text.casefold())
        events = self.api_events()
        self.assertEqual(len(events), 3)
        for path in ("/status", "/admin"):
            self.assertEqual(self.client.request("GET", path)[0], 200, path)
        status, health = self.client.json("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["service_version"], "1.7.0")
        self.assertTrue(health["infographics"]["enabled"])

    def test_ical_unchanged_by_infographics(self) -> None:
        before = self.client.request("GET", "/pokemon-go-de.ics")[2].decode()
        self.seed("1300000000000001000", ocr_text="4. NOV - 8. NOV\nGIRATINA\nSchwäche")
        self.api_events()
        app.invalidate_calendar_cache()
        after = self.client.request("GET", "/pokemon-go-de.ics")[2].decode()
        strip = lambda value: "\n".join(line for line in value.splitlines() if not line.startswith(("DTSTAMP", "LAST-MODIFIED")))
        self.assertEqual(strip(before), strip(after))

    def test_calendar_survives_broken_infographic_data(self) -> None:
        Path(self.settings["metadata_file"]).write_text("{kaputt", encoding="utf-8")
        events = self.api_events()
        self.assertEqual(len(events), 3)
        self.assertEqual(self.client.request("GET", "/pokemon-go-de.ics")[0], 200)

    def test_disabled_infographics(self) -> None:
        config = json.loads(json.dumps(self.config))
        config["infographics"]["enabled"] = False
        helpers.write_config(config)
        self.seed("1300000000000001100", ocr_text="4. NOV - 8. NOV GIRATINA Schwäche")
        events = self.api_events()
        self.assertTrue(all("infographics" not in event for event in events.values()))
        status, health = self.client.json("GET", "/health")
        self.assertFalse(health["infographics"]["enabled"])

    # --- automatic matching and display ------------------------------------
    def test_auto_match_is_shown_on_event(self) -> None:
        record_id = self.seed("1300000000000001200", ocr_text="4. NOV - 8. NOV\n#487 GIRATINA\nSchwäche\nTrainer benötigt")
        events = self.api_events()
        giratina = events["giratina-altered-forme-in-5-star-raid-battles-november-2026"]
        self.assertEqual([i["id"] for i in giratina["infographics"]], [record_id])
        self.assertNotIn("infographics", events["shadow-tornadus-incarnate-forme-in-shadow-raids-november-4-2026"])
        status, payload = self.client.json("GET", "/api/events")
        self.assertEqual(payload["meta"]["infographics"]["attribution"]["creator"], "G47IX")
        status, headers, image = self.client.request("GET", giratina["infographics"][0]["url"])
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "image/jpeg")
        self.assertTrue(image.startswith(b"\xff\xd8\xff"))
        stored = self.store.get(record_id)
        self.assertEqual(stored["matching"]["status"], "matched")
        self.assertEqual(stored["matching"]["method"], "auto")

    def test_unmatched_image_not_public(self) -> None:
        record_id = self.seed("1300000000000001300", content="Hallo zusammen")
        self.api_events()
        self.assertEqual(self.client.request("GET", f"/infographic/{record_id}")[0], 404)
        self.assertEqual(self.client.request("GET", "/infographic/../../etc/passwd")[0], 404)
        self.client.login()
        self.assertEqual(self.client.request("GET", f"/infographic/{record_id}")[0], 200)

    # --- admin ------------------------------------------------------------------
    def test_admin_endpoints_require_authentication(self) -> None:
        record_id = self.seed("1300000000000001400")
        self.assertEqual(self.client.json("GET", "/api/admin/infographics")[0], 401)
        self.assertEqual(self.client.json("POST", f"/api/admin/infographics/{record_id}/reject", {})[0], 401)
        self.assertEqual(self.client.json("DELETE", f"/api/admin/infographics/{record_id}")[0], 401)
        self.client.login()
        self.assertEqual(self.client.json("POST", f"/api/admin/infographics/{record_id}/reject", {}, csrf=False)[0], 403)
        self.assertEqual(self.store.get(record_id)["matching"]["status"], "pending")

    def test_17_manual_assignment_in_admin(self) -> None:
        record_id = self.seed("1300000000000001500", ocr_text="ZWIELICHT\nWÜNSCHE\n30 NOV\n10:00", posted="2026-11-06T18:00:00+00:00")
        self.api_events()
        self.assertEqual(self.store.get(record_id)["matching"]["status"], "manual_review")
        self.client.login()
        status, overview = self.client.json("GET", "/api/admin/infographics")
        self.assertEqual(status, 200)
        item = next(i for i in overview["items"] if i["id"] == record_id)
        self.assertEqual(item["status"], "manual_review")
        self.assertEqual(item["candidates"][0]["event_id"], "twilight-wishes-2026")
        self.assertEqual(len(overview["events"]), 3)
        self.assertEqual(overview["summary"]["manual_review"], 1)

        status, result = self.client.json("POST", f"/api/admin/infographics/{record_id}/assign", {"event_id": "twilight-wishes-2026"})
        self.assertEqual((status, result["status"]), (200, "matched"))
        events = self.api_events()
        self.assertEqual(events["twilight-wishes-2026"]["infographics"][0]["id"], record_id)
        stored = self.store.get(record_id)["matching"]
        self.assertEqual((stored["method"], stored["locked"]), ("manual", True))

        # Change to another event, then remove, reject and re-evaluate
        self.client.json("POST", f"/api/admin/infographics/{record_id}/assign", {"event_id": "shadow-tornadus-incarnate-forme-in-shadow-raids-november-4-2026"})
        events = self.api_events()
        self.assertNotIn("infographics", events["twilight-wishes-2026"])
        self.assertIn("infographics", events["shadow-tornadus-incarnate-forme-in-shadow-raids-november-4-2026"])
        self.assertEqual(self.client.json("POST", f"/api/admin/infographics/{record_id}/unassign", {})[1]["status"], "manual_review")
        self.assertEqual(self.client.json("POST", f"/api/admin/infographics/{record_id}/reject", {})[1]["status"], "rejected")
        self.assertTrue(all("infographics" not in event for event in self.api_events().values()))
        self.assertEqual(self.client.json("POST", f"/api/admin/infographics/{record_id}/rematch", {})[1]["status"], "manual_review")
        status, error = self.client.json("POST", f"/api/admin/infographics/{record_id}/assign", {"event_id": "gibt-es-nicht"})
        self.assertEqual(status, 400)

    def test_18_delete_infographic(self) -> None:
        record_id = self.seed("1300000000000001600", ocr_text="4. NOV - 8. NOV GIRATINA Schwäche")
        path = self.store.resolve_file(self.store.get(record_id)["attachment"]["local_path"])
        self.assertTrue(path.exists())
        self.client.login()
        status, _ = self.client.json("DELETE", f"/api/admin/infographics/{record_id}")
        self.assertEqual(status, 200)
        self.assertIsNone(self.store.get(record_id))
        self.assertFalse(path.exists())
        self.assertIn(record_id, self.store.read()["deleted"])
        self.assertTrue(all("infographics" not in event for event in self.api_events().values()))
        self.assertEqual(self.client.json("DELETE", f"/api/admin/infographics/{record_id}")[0], 404)
        # The worker must not import the deleted image again
        collector = InfographicCollector(self.settings, self.store, FakeDiscordClient(), GUILD_ID, [CHANNEL_ID], {WEBHOOK_ID}, downloader=FakeDownloader())
        again = collector.process_message(CHANNEL_ID, message("1300000000000001600", "", [attachment("1400000000000001600")]))
        self.assertEqual(again, [])

    def test_cleanup_rejected(self) -> None:
        first = self.seed("1300000000000001700")
        second = self.seed("1300000000000001701")
        self.client.login()
        self.client.json("POST", f"/api/admin/infographics/{first}/reject", {})
        status, result = self.client.json("POST", "/api/admin/infographics/cleanup", {"mode": "rejected"})
        self.assertEqual((status, result["removed"]), (200, 1))
        self.assertIsNone(self.store.get(first))
        self.assertIsNotNone(self.store.get(second))

    def test_19_assign_to_manual_event(self) -> None:
        self.client.login()
        status, created = self.client.json("POST", "/api/admin/events", {
            "title": "Raid-Treffen Augsburg",
            "type": "manual-raid",
            "all_day": False,
            "start": "2026-11-15T17:00",
            "end": "2026-11-15T20:00",
            "pokemon": "Rayquaza",
        })
        self.assertEqual(status, 201)
        manual_id = created["event"]["id"]
        record_id = self.seed("1300000000000001800", content="Treffen am Rathausplatz")
        status, result = self.client.json("POST", f"/api/admin/infographics/{record_id}/assign", {"event_id": manual_id})
        self.assertEqual((status, result["status"]), (200, "matched"))
        events = self.api_events()
        self.assertTrue(events[manual_id]["isManual"])
        self.assertEqual(events[manual_id]["infographics"][0]["id"], record_id)

    def test_admin_password_from_environment(self) -> None:
        import os

        os.environ["ADMIN_PASSWORD"] = "aus-der-env-datei"
        try:
            status, _ = self.client.json("POST", "/api/admin/login", {"password": helpers.TEST_PASSWORD})
            self.assertEqual(status, 401)
            status, _ = self.client.json("POST", "/api/admin/login", {"password": "aus-der-env-datei"})
            self.assertEqual(status, 200)
        finally:
            os.environ.pop("ADMIN_PASSWORD", None)


if __name__ == "__main__":
    unittest.main()
