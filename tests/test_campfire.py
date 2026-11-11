from __future__ import annotations

import unittest
import urllib.error

import campfire
from tests import helpers  # noqa: F401  (sets up import paths and environment)
from tests.test_web import WebBase

MEETUP_ID = "3a8fe749-7ac3-41e1-a6e4-d4c0e4f2cad3"
PAGE = f"""<!DOCTYPE html><html><head><title>Wild Area 2026 Gathering! Day 1</title>
<meta name="description" content="mighty spawns kommen zur&#252;ck &amp; hundos"/>
<meta property="og:title" content="Wild Area 2026 Gathering! Day 1"/>
<meta property="og:description" content="mighty spawns kommen zur&#252;ck &amp; hundos"/>
<meta property="og:image" content="https://niantic-social-api.nianticlabs.com/images/16bee88ecc8a3c4d"/>
</head><body><div id="root"></div></body></html>""".encode()


def fetcher(final_url: str = f"https://campfire.scopely.com/discover/meetup/{MEETUP_ID}", page: bytes = PAGE):
    calls = []

    def fetch(url, timeout):
        calls.append(url)
        return final_url, page

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


class CampfireParseTest(unittest.TestCase):
    def test_link_from_share_text(self) -> None:
        text = "Komm zu meinem Meetup „Wild Area“ auf Campfire! https://cmpf.re/DjdBwz."
        self.assertEqual(campfire.extract_link(text), "https://cmpf.re/DjdBwz")
        self.assertEqual(campfire.extract_link("cmpf.re/DjdBwz"), "https://cmpf.re/DjdBwz")

    def test_rejects_other_hosts(self) -> None:
        for text in ("https://example.com/discover/meetup/x", "http://cmpf.re/abc", "kein Link", ""):
            with self.assertRaises(ValueError):
                campfire.extract_link(text)

    def test_fetch_meetup(self) -> None:
        fetch = fetcher()
        meetup = campfire.fetch_meetup("https://cmpf.re/DjdBwz", fetch=fetch)
        self.assertEqual(fetch.calls, ["https://cmpf.re/DjdBwz"])
        self.assertEqual(meetup["id"], MEETUP_ID)
        self.assertEqual(meetup["title"], "Wild Area 2026 Gathering! Day 1")
        self.assertEqual(meetup["description"], "mighty spawns kommen zurück & hundos")
        self.assertEqual(meetup["url"], f"https://campfire.scopely.com/discover/meetup/{MEETUP_ID}")
        self.assertTrue(meetup["image"].startswith("https://"))

    def test_link_must_end_on_meetup_page(self) -> None:
        with self.assertRaises(ValueError):
            campfire.fetch_meetup("https://cmpf.re/abc", fetch=fetcher("https://campfire.scopely.com/discover"))
        with self.assertRaises(ValueError):
            campfire.fetch_meetup("https://cmpf.re/abc", fetch=fetcher(page=b"<html><title>Campfire</title></html>"))

    def test_redirect_to_foreign_host_is_blocked(self) -> None:
        handler = campfire._AllowlistRedirect()
        with self.assertRaises(urllib.error.HTTPError):
            handler.redirect_request(None, None, 302, "Found", {}, "https://evil.example/discover/meetup/x")

    def test_unreachable(self) -> None:
        def fetch(url, timeout):
            raise urllib.error.URLError("timeout")

        with self.assertRaises(RuntimeError):
            campfire.fetch_meetup("https://cmpf.re/abc", fetch=fetch)


class CampfireEndpointTest(WebBase):
    def setUp(self) -> None:
        super().setUp()
        self._original = campfire.default_fetch
        campfire.default_fetch = fetcher()

    def tearDown(self) -> None:
        campfire.default_fetch = self._original

    def test_requires_login(self) -> None:
        status, _ = self.client.json("POST", "/api/admin/campfire", {"url": "https://cmpf.re/DjdBwz"})
        self.assertEqual(status, 401)

    def test_import_and_duplicate_detection(self) -> None:
        self.client.login()
        status, data = self.client.json("POST", "/api/admin/campfire", {"url": "https://cmpf.re/DjdBwz"})
        self.assertEqual(status, 200, data)
        meetup = data["meetup"]
        self.assertEqual(meetup["title"], "Wild Area 2026 Gathering! Day 1")
        self.assertIsNone(meetup["existingEventId"])

        status, created = self.client.json("POST", "/api/admin/events", {
            "title": meetup["title"], "type": "manual-meetup", "all_day": False,
            "start": "2026-11-14T09:45", "end": "2026-11-14T18:15",
            "description": meetup["description"], "location": "Königsplatz", "url": meetup["url"],
        })
        self.assertEqual(status, 201, created)
        status, data = self.client.json("POST", "/api/admin/campfire", {"url": "https://cmpf.re/DjdBwz"})
        self.assertEqual(data["meetup"]["existingEventId"], created["event"]["id"])

    def test_invalid_link(self) -> None:
        self.client.login()
        status, data = self.client.json("POST", "/api/admin/campfire", {"url": "https://example.com/x"})
        self.assertEqual(status, 400)
        self.assertIn("Campfire-Link", data["error"])


if __name__ == "__main__":
    unittest.main()
