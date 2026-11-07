"""Hub data (raids, eggs, research, weather) and the filtered iCal feed."""

from __future__ import annotations

import json
import unittest
import urllib.request

from tests import helpers
from tests.test_web import WebBase

import app
import hub

RAIDS = [
    {"name": "Giratina (Altered)", "tier": "5-Star Raids", "canBeShiny": True,
     "types": [{"name": "ghost"}, {"name": "dragon"}],
     "combatPower": {"normal": {"min": 1848, "max": 1931}, "boosted": {"min": 2310, "max": 2414}},
     "boostedWeather": [{"name": "fog"}, {"name": "windy"}],
     "image": "https://cdn.leekduck.com/assets/img/pokemon_icons/pm487.icon.png"},
    {"name": "Frillish (Female)", "tier": "1-Star Raids", "canBeShiny": True,
     "types": [{"name": "water"}, {"name": "ghost"}],
     "combatPower": {"normal": {"min": 668, "max": 718}, "boosted": {"min": 835, "max": 898}},
     "boostedWeather": [{"name": "rainy"}, {"name": "fog"}],
     "image": "https://cdn.leekduck.com/assets/img/pokemon_icons/pm592.icon.png"},
    {"name": "Mega Sableye", "tier": "Mega Raids", "canBeShiny": True, "types": [{"name": "dark"}],
     "combatPower": {"normal": {"min": 789, "max": 843}}, "boostedWeather": [{"name": "fog"}],
     "image": "https://example.com/not-allowed.png"},
]
EGGS = [
    {"name": "Pichu", "eggType": "2 km", "isAdventureSync": False, "isGiftExchange": False, "canBeShiny": True,
     "isRegional": False, "image": "https://cdn.leekduck.com/a.png", "combatPower": {"min": 1, "max": 1}},
    {"name": "Alolan Geodude", "eggType": "7 km", "isAdventureSync": False, "isGiftExchange": True, "canBeShiny": True,
     "isRegional": False, "image": "https://cdn.leekduck.com/b.png", "combatPower": {"min": 1, "max": 1}},
    {"name": "Bulbasaur", "eggType": "1 km", "isAdventureSync": False, "isGiftExchange": False, "canBeShiny": True,
     "isRegional": False, "image": "https://cdn.leekduck.com/c.png", "combatPower": {"min": 1, "max": 1}},
    {"name": "Riolu", "eggType": "5 km", "isAdventureSync": True, "isGiftExchange": False, "canBeShiny": True,
     "isRegional": False, "image": "https://cdn.leekduck.com/d.png", "combatPower": {"min": 1, "max": 1}},
]
RESEARCH = [
    {"text": "<span>Catch 10 Dragon-type Pokémon</span>", "rewards": [{"name": "Trapinch", "image": "https://cdn.leekduck.com/e.png", "canBeShiny": False, "combatPower": {"min": 507, "max": 546}}] * 2},
    {"text": "<span>Win 3 raids</span>", "rewards": [{"name": "Pikachu wearing a hat and capelet", "image": "https://cdn.leekduck.com/f.png", "canBeShiny": True}]},
    {"text": "<span>Make 3 Excellent Throws in a row</span>", "rewards": [{"name": "Beldum", "image": "https://cdn.leekduck.com/g.png", "canBeShiny": True}]},
    {"text": "<span>Do something completely new with Fire-type Pokémon</span>", "rewards": []},
]
WEATHER = {
    "current": {"time": "2026-11-07T09:30", "weather_code": 61, "wind_speed_10m": 2.6, "wind_gusts_10m": 15.5, "temperature_2m": 5.0, "is_day": 1},
    "hourly": {"time": ["2026-11-07T09:00", "2026-11-07T10:00", "2026-11-07T11:00", "2026-11-07T12:00"],
               "weather_code": [61, 3, 0, 0], "wind_speed_10m": [2, 3, 30, 4], "wind_gusts_10m": [10, 10, 45, 10], "is_day": [1, 1, 1, 0]},
}


def fake_fetch(url: str, timeout: int) -> bytes:
    if "raids" in url:
        return json.dumps(RAIDS).encode()
    if "eggs" in url:
        return json.dumps(EGGS).encode()
    if "research" in url:
        return json.dumps(RESEARCH).encode()
    if "open-meteo" in url:
        return json.dumps(WEATHER).encode()
    raise AssertionError(url)


class WeatherMappingTest(unittest.TestCase):
    settings = hub.hub_settings({})["weather"]

    def test_conditions(self) -> None:
        cases = {0: "sunny", 2: "partly cloudy", 3: "cloudy", 45: "fog", 61: "rainy", 80: "rainy", 95: "rainy", 71: "snow"}
        for code, expected in cases.items():
            self.assertEqual(hub.weather_condition(code, 5, 10, self.settings), expected, code)
        self.assertEqual(hub.weather_condition(0, 30, 10, self.settings), "windy")
        self.assertEqual(hub.weather_condition(3, 5, 45, self.settings), "windy")
        self.assertEqual(hub.weather_condition(61, 40, 60, self.settings), "rainy")  # rain wins over wind

    def test_boosts_and_night_label(self) -> None:
        self.assertEqual([t["label"] for t in hub.condition_payload("rainy", True)["boosted"]], ["Wasser", "Elektro", "Käfer"])
        self.assertEqual(hub.condition_payload("sunny", False)["label"], "Klar")
        self.assertEqual([t["label"] for t in hub.condition_payload("windy", True)["boosted"]], ["Drache", "Flug", "Psycho"])

    def test_research_translation(self) -> None:
        self.assertEqual(hub.translate_research_task("Catch 10 Dragon-type Pokémon"), "Fange 10 Pokémon vom Typ Drache")
        self.assertEqual(hub.translate_research_task("Power up Steel-type Pokémon 5 times"), "Verstärke Pokémon vom Typ Stahl 5-mal")
        self.assertEqual(hub.translate_research_task("Spin 5 PokéStops or Gyms"), "Drehe 5 PokéStops oder Arenen")
        self.assertEqual(hub.translate_research_task("Make 3 Great Throws in a row"), "Schaffe 3 großartige Würfe hintereinander")
        self.assertEqual(hub.translate_research_task("Unknown thing with Fire-type Pokémon"), "Unknown thing with Feuer-Typ Pokémon")


class HubPayloadTest(WebBase):
    def setUp(self) -> None:
        super().setUp()
        config = json.loads(json.dumps(self.config))
        config["hub"] = {"cache_file": str(self.data_dir / "hub-cache.json")}
        helpers.write_config(config)
        app.HUB.__init__()
        app.HUB.fetch = fake_fetch

    def test_hub_endpoint(self) -> None:
        status, data = self.client.json("GET", "/api/hub")
        self.assertEqual(status, 200)
        self.assertEqual([tier["tier"] for tier in data["raids"]], ["Stufe 5", "Mega", "Stufe 1"])
        giratina = data["raids"][0]["bosses"][0]
        self.assertEqual(giratina["name"], "Giratina (Wandelform)")
        self.assertEqual([t["label"] for t in giratina["types"]], ["Geist", "Drache"])
        self.assertEqual(giratina["cpBoosted"], {"min": 2310, "max": 2414})
        self.assertTrue(giratina["image"].startswith("/pokemon-image/"))
        self.assertFalse(giratina["boostedNow"])
        frillish = data["raids"][2]["bosses"][0]
        self.assertEqual(frillish["name"], "Quabbel (weiblich)")
        self.assertTrue(frillish["boostedNow"])  # raining
        self.assertIsNone(data["raids"][1]["bosses"][0]["image"])  # host not allowed

        self.assertEqual([group["label"] for group in data["eggs"]], ["1 km", "2 km", "5 km · Abenteuer-Sync", "7 km · Aus Geschenken"])
        self.assertEqual(data["eggs"][0]["pokemon"][0]["name"], "Bisasam")
        self.assertEqual(data["eggs"][3]["pokemon"][0]["name"], "Alola-Kleinstein")

        self.assertEqual(data["research"][0]["task"], "Fange 10 Pokémon vom Typ Drache")
        self.assertEqual([r["name"] for r in data["research"][0]["rewards"]], ["Knacklion"])
        self.assertEqual(data["research"][1]["rewards"][0]["name"], "Pikachu mit Hut und Umhang")

        weather = data["weather"]
        self.assertEqual((weather["location"], weather["current"]["label"]), ("Augsburg", "Regen"))
        self.assertEqual([hour["label"] for hour in weather["forecast"]], ["Bewölkt", "Windig", "Klar"])
        self.assertEqual(data["errors"], {})

    def test_sources_down_uses_disk_cache(self) -> None:
        self.client.json("GET", "/api/hub")
        app.HUB.__init__()

        def broken(url, timeout):
            raise RuntimeError("offline")

        app.HUB.fetch = broken
        status, data = self.client.json("GET", "/api/hub")
        self.assertEqual(status, 200)
        self.assertEqual(len(data["raids"]), 3)
        self.assertIn("raids", data["errors"])
        self.assertIsNone(data["weather"])

    def test_hub_can_be_disabled(self) -> None:
        config = json.loads(json.dumps(self.config))
        config["hub"] = {"enabled": False}
        helpers.write_config(config)
        self.assertEqual(self.client.json("GET", "/api/hub")[1], {"enabled": False})


class FilteredCalendarTest(WebBase):
    def ics(self, query: str = "") -> str:
        status, headers, body = self.client.request("GET", f"/pokemon-go-de.ics{query}")
        self.assertEqual(status, 200)
        return body.decode("utf-8")

    def test_groups_filter(self) -> None:
        self.client.login()
        self.client.json("POST", "/api/admin/events", {
            "title": "Raid-Treffen Rathausplatz", "type": "manual-raid", "all_day": False,
            "start": "2026-11-15T17:00", "end": "2026-11-15T20:00",
        })
        app.invalidate_calendar_cache()
        full = self.ics()
        self.assertEqual(full.count("BEGIN:VEVENT"), 4)
        raids = self.ics("?groups=raids")
        self.assertEqual(raids.count("BEGIN:VEVENT"), 2)
        self.assertIn("Raids)", raids)
        self.assertNotIn("Twilight", raids)
        local = self.ics("?groups=local")
        self.assertEqual(local.count("BEGIN:VEVENT"), 1)
        self.assertIn("Raid-Treffen Rathausplatz", local)
        self.assertEqual(self.ics("?groups=quatsch").count("BEGIN:VEVENT"), 4)
        all_groups = ",".join(app.GROUP_IDS)
        self.assertEqual(self.ics(f"?groups={all_groups}"), full)

    def test_events_carry_group(self) -> None:
        events = self.api_events()
        self.assertEqual(events["twilight-wishes-2026"]["group"], "events")
        self.assertEqual(events["giratina-altered-forme-in-5-star-raid-battles-november-2026"]["group"], "raids")
        status, payload = self.client.json("GET", "/api/events")
        self.assertEqual([group["id"] for group in payload["meta"]["groups"]], app.GROUP_IDS)


if __name__ == "__main__":
    unittest.main()


class LocationSearchTest(unittest.TestCase):
    def setUp(self) -> None:
        import locations

        self.index = locations.LocationIndex(helpers.ROOT / "plz-de.json")

    def labels(self, query):
        results, more = self.index.search(query)
        return [item["label"] for item in results], more

    def test_postcode(self) -> None:
        self.assertEqual(self.labels("86150")[0], ["86150 Augsburg"])
        labels, more = self.labels("8615")
        self.assertTrue(all(label.startswith("8615") for label in labels))
        self.assertIn("86157 Augsburg", labels)

    def test_place_name_with_umlauts(self) -> None:
        self.assertEqual(self.labels("Neusäß")[0], ["86356 Neusäß"])
        self.assertEqual(self.labels("neusaess")[0], ["86356 Neusäß"])
        self.assertEqual(self.labels("Königsbrunn")[0], ["86343 Königsbrunn"])

    def test_city_with_many_postcodes_lists_all(self) -> None:
        labels, more = self.labels("Augsburg")
        self.assertEqual(labels[0], "86150 Augsburg")
        self.assertEqual(sum(1 for label in labels if label.endswith(" Augsburg")), 14)
        self.assertFalse(any("Agentur" in label or "Amtsgericht" in label for label in labels))

    def test_postcode_and_place_combined(self) -> None:
        self.assertEqual(self.labels("90762 Fürth")[0], ["90762 Fürth"])
        self.assertEqual(self.labels("Fürth 64658")[0], ["64658 Fürth"])

    def test_too_short(self) -> None:
        self.assertEqual(self.labels("x"), ([], False))


class LocationEndpointTest(WebBase):
    def setUp(self) -> None:
        super().setUp()
        app.HUB.__init__()
        app.HUB.fetch = fake_fetch
        with app.PUBLIC_RATE_LOCK:
            app.PUBLIC_RATE.clear()

    def test_location_search_endpoint(self) -> None:
        status, data = self.client.json("GET", "/api/locations?q=86368")
        self.assertEqual(status, 200)
        self.assertEqual(data["results"][0]["label"], "86368 Gersthofen")
        self.assertAlmostEqual(data["results"][0]["latitude"], 48.42, places=1)

    def test_fallback_to_open_meteo_for_foreign_places(self) -> None:
        def fetch(url, timeout):
            self.assertIn("geocoding-api.open-meteo.com", url)
            return json.dumps({"results": [{"name": "Innsbruck", "latitude": 47.26, "longitude": 11.39,
                                            "admin1": "Tirol", "country": "Österreich", "country_code": "AT"}]}).encode()

        app.HUB.fetch = fetch
        status, data = self.client.json("GET", "/api/locations?q=Innsbruck")
        self.assertEqual((data["source"], data["results"][0]["label"]), ("Open-Meteo", "Innsbruck"))

    def test_weather_for_chosen_place(self) -> None:
        status, data = self.client.json("GET", "/api/weather?lat=48.4244&lon=10.8823&name=86368%20Gersthofen")
        self.assertEqual(status, 200)
        self.assertEqual((data["location"], data["current"]["label"]), ("86368 Gersthofen", "Regen"))
        self.assertEqual(self.client.json("GET", "/api/weather?lat=999&lon=10")[0], 400)
        self.assertEqual(self.client.json("GET", "/api/weather?lat=abc&lon=10")[0], 400)

    def test_rate_limit(self) -> None:
        app.PUBLIC_RATE_LIMIT = (3, 600)
        try:
            codes = [self.client.json("GET", "/api/weather?lat=48.4&lon=10.9&name=X")[0] for _ in range(4)]
        finally:
            app.PUBLIC_RATE_LIMIT = (60, 600)
        self.assertEqual(codes, [200, 200, 200, 429])
