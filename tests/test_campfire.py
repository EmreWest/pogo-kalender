from __future__ import annotations

import unittest
import urllib.error

import campfire
from tests import helpers  # noqa: F401  (sets up import paths and environment)
from tests.test_web import WebBase

import ocr


def ocr_languages() -> str:
    import subprocess

    try:
        return subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return ""


def font(size: int):
    from PIL import ImageFont

    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # very old Pillow
        return ImageFont.load_default()

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


SHOT_DE = """Wild Area 2026 Gathering! Day:
1

mighty spawns kommen zurück und damit
auch die Glorreiche jagd auf hundos &
shundos in der wildnis :D

14. Nov. 2026
09:45 - 18:15

©)  Königsplatz, Augsburg, Bearbeiten
BY, Deutschland
48.366268, 10.894629

) Host
DaniMayestik

Gruppe
Augsburg Community Events
"""

SHOT_SPLIT = """Gladiantri Community Day
Meetup

21. Nov. 2026

Bearbeiten
14:00 - 17:00
©) Thormannbrunnen,
Augsburg, BY, .
Bearbeiten

Deutschland
48.366268, 10.893734

N Host
"""

SHOT_EN = """Late Night Raid Hour

Nov 10, 2026
10:30 PM - 12:15 AM

@ Brückenstraße 5, Augsburg, BY, Germany
48.377828, 10.902163
Host
"""


class ScreenshotParseTest(unittest.TestCase):
    def test_german_screenshot(self) -> None:
        result = campfire.parse_screenshot_text(SHOT_DE)
        self.assertEqual(result["title"], "Wild Area 2026 Gathering! Day 1")
        self.assertTrue(result["description"].startswith("mighty spawns kommen zurück"))
        self.assertEqual((result["start"], result["end"]), ("2026-11-14T09:45", "2026-11-14T18:15"))
        self.assertEqual(result["location"], "Königsplatz, Augsburg, BY, Deutschland")
        self.assertEqual(result["coordinates"], [48.366268, 10.894629])

    def test_noise_between_lines(self) -> None:
        result = campfire.parse_screenshot_text(SHOT_SPLIT)
        self.assertEqual(result["title"], "Gladiantri Community Day Meetup")
        self.assertEqual((result["start"], result["end"]), ("2026-11-21T14:00", "2026-11-21T17:00"))
        self.assertEqual(result["location"], "Thormannbrunnen, Augsburg, BY, Deutschland")

    def test_english_and_past_midnight(self) -> None:
        result = campfire.parse_screenshot_text(SHOT_EN)
        self.assertEqual((result["start"], result["end"]), ("2026-11-10T22:30", "2026-11-11T00:15"))
        self.assertEqual(result["location"], "Brückenstraße 5, Augsburg, BY, Germany")

    def test_without_date(self) -> None:
        result = campfire.parse_screenshot_text("Irgendein Bild\nohne Termin")
        self.assertIsNone(result["start"])


class ScreenshotOcrTest(unittest.TestCase):
    @unittest.skipUnless(ocr.tesseract_available() and ocr.PIL_AVAILABLE and "deu" in ocr_languages(), "Tesseract mit Deutsch nicht installiert")
    def test_synthetic_phone_screenshot(self) -> None:
        import io
        from PIL import Image, ImageDraw, ImageFont

        image = Image.new("RGB", (1179, 2556), (255, 255, 255))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 1179, 670), fill=(240, 228, 200))  # map
        draw.text((60, 300), "Rathaus", fill=(90, 90, 120), font=font(48))
        draw.text((70, 740), "Raid-Treffen am Brunnen", fill=(40, 55, 90), font=font(64))
        draw.rounded_rectangle((70, 900, 1105, 1400), radius=40, fill=(240, 242, 245))
        draw.text((260, 940), "14. Nov. 2026", fill=(40, 55, 90), font=font(52))
        draw.text((260, 1020), "09:45 - 18:15", fill=(120, 125, 140), font=font(42))
        draw.text((810, 980), "Bearbeiten", fill=(80, 190, 235), font=font(46))
        draw.text((260, 1160), "Königsplatz, Augsburg,", fill=(40, 55, 90), font=font(52))
        draw.text((260, 1230), "BY, Deutschland", fill=(40, 55, 90), font=font(52))
        draw.text((260, 1310), "48.366268, 10.894629", fill=(120, 125, 140), font=font(42))
        draw.text((260, 1520), "Host", fill=(120, 125, 140), font=font(46))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")

        result = campfire.read_screenshot(buffer.getvalue())
        self.assertEqual((result["start"], result["end"]), ("2026-11-14T09:45", "2026-11-14T18:15"))
        # The bundled test font is rough; umlauts are not checked here.
        self.assertRegex(result["location"], r"^K.{1,2}nigsplatz, Augsburg, BY, Deutschland$")
        self.assertEqual(result["title"], "Raid-Treffen am Brunnen")

    def test_rejects_non_images(self) -> None:
        if not (ocr.tesseract_available() and ocr.PIL_AVAILABLE):
            self.skipTest("Tesseract oder Pillow nicht installiert")
        with self.assertRaises(ValueError):
            campfire.read_screenshot(b"GIF89a kein Screenshot")


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

    def test_link_and_screenshot(self) -> None:
        import base64
        original = campfire.read_screenshot
        received = []

        def fake(data, languages="deu", timeout=60):
            received.append(data)
            return campfire.parse_screenshot_text(SHOT_DE)

        campfire.read_screenshot = fake
        try:
            self.client.login()
            image = "data:image/png;base64," + base64.b64encode(b"\x89PNG-test").decode()
            status, data = self.client.json("POST", "/api/admin/campfire", {"url": "https://cmpf.re/DjdBwz", "image": image})
            self.assertEqual(status, 200, data)
            meetup = data["meetup"]
            self.assertEqual(received, [b"\x89PNG-test"])
            self.assertEqual(meetup["description"], "mighty spawns kommen zurück & hundos")  # from the link
            self.assertEqual((meetup["start"], meetup["location"]), ("2026-11-14T09:45", "Königsplatz, Augsburg, BY, Deutschland"))
            self.assertEqual(meetup["warnings"], [])

            status, data = self.client.json("POST", "/api/admin/campfire", {"image": image})
            self.assertEqual(data["meetup"]["title"], "Wild Area 2026 Gathering! Day 1")  # from the screenshot
            status, data = self.client.json("POST", "/api/admin/campfire", {"image": "kein-bild!"})
            self.assertEqual(status, 400)
        finally:
            campfire.read_screenshot = original

    def test_location_with_coordinates_in_calendar(self) -> None:
        self.client.login()
        status, data = self.client.json("POST", "/api/admin/events", {
            "title": "Raid Hour Treffen", "type": "campfire-meetup", "all_day": False,
            "start": "2026-11-10T18:00", "end": "2026-11-10T19:00",
            "location": "Brückenstraße 5, Augsburg, BY, Deutschland", "coordinates": "48.377828, 10.902163",
        })
        self.assertEqual(status, 201, data)
        self.assertEqual(data["event"]["geo"], [48.377828, 10.902163])

        status, payload = self.client.json("GET", "/api/events")
        event = next(item for item in payload["events"] if item["id"] == data["event"]["id"])
        self.assertEqual(event["geo"], [48.377828, 10.902163])
        self.assertEqual(event["category"], "Campfire-Meetup")
        self.assertEqual(event["routeUrl"], "https://www.google.com/maps/dir/?api=1&destination=48.377828,10.902163")

        status, _, body = self.client.request("GET", "/pokemon-go-de.ics")
        text = body.decode("utf-8").replace("\r\n ", "")
        self.assertIn("LOCATION:Brückenstraße 5\\, Augsburg\\, BY\\, Deutschland", text)
        self.assertIn("GEO:48.377828;10.902163", text)
        self.assertIn('X-APPLE-STRUCTURED-LOCATION;VALUE=URI;X-ADDRESS="Brückenstraße 5, Augsburg, BY, Deutschland"', text)
        self.assertIn(":geo:48.377828,10.902163", text)
        self.assertIn("Route: https://www.google.com/maps/dir/?api=1&destination=48.377828\\,10.902163", text)

        for bad in ("48.3", "abc, def", "95, 10", "0, 0"):
            status, data = self.client.json("POST", "/api/admin/events", {
                "title": "X", "all_day": True, "start": "2026-11-10", "end": "2026-11-10", "coordinates": bad,
            })
            self.assertEqual(status, 400, bad)

    def test_location_without_coordinates_gets_route_by_text(self) -> None:
        self.client.login()
        self.client.json("POST", "/api/admin/events", {
            "title": "Treffen", "all_day": True, "start": "2026-11-10", "end": "2026-11-10", "location": "Königsplatz, Augsburg",
        })
        status, _, body = self.client.request("GET", "/pokemon-go-de.ics")
        text = body.decode("utf-8").replace("\r\n ", "")
        self.assertIn("Route: https://www.google.com/maps/dir/?api=1&destination=K%C3%B6nigsplatz%2C%20Augsburg", text)
        self.assertNotIn("GEO:", text)

    def test_invalid_link(self) -> None:
        self.client.login()
        status, data = self.client.json("POST", "/api/admin/campfire", {"url": "https://example.com/x"})
        self.assertEqual(status, 400)
        self.assertIn("Campfire-Link", data["error"])


if __name__ == "__main__":
    unittest.main()
