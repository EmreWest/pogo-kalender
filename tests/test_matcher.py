"""Event matching: Pokémon names, dates, types and ambiguity handling."""

from __future__ import annotations

import unittest
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from tests import helpers  # noqa: F401  (sets environment before importing app)

import app
import event_matcher
from infographic_store import infographic_settings

TZ = ZoneInfo("Europe/Berlin")
SETTINGS = infographic_settings({})
POSTED = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)


def species_ids(text: str) -> set[int]:
    return {int(s["id"]) for s in app.pokemon_species_matches(text)}


def event(event_id, title, start, end, event_type, source_title="", manual=False, all_day=False):
    return {
        "id": event_id,
        "title": title,
        "sourceTitle": source_title or title,
        "start": start,
        "end": end,
        "eventType": event_type,
        "isManual": manual,
        "allDay": all_day,
        "pokemonImages": [],
    }


GIRATINA = event(
    "giratina-raids", "Giratina (Altered Forme) in Stufe-5-Raids",
    "2026-11-04T06:00:00+01:00", "2026-11-08T22:00:00+01:00", "raid-battles",
    "Giratina (Altered Forme) in 5-star Raid Battles",
)
TORNADUS = event(
    "tornadus-raids", "Crypto-Boreos (Incarnate Forme) in Crypto-Raids",
    "2026-11-04T06:00:00+01:00", "2026-11-08T22:00:00+01:00", "raid-battles",
    "Shadow Tornadus (Incarnate Forme) in Shadow Raids",
)
SPOTLIGHT = event(
    "spotlight-teddiursa", "Teddiursa-Rampenlichtstunde",
    "2026-11-05T18:00:00+01:00", "2026-11-05T19:00:00+01:00", "pokemon-spotlight-hour",
    "Teddiursa Spotlight Hour",
)
CHARIZARD_RAID_DAY = event(
    "mega-charizard-x-raid-day", "Mega Glurak X Raid-Tag",
    "2026-11-08T14:00:00+01:00", "2026-11-09T17:00:00+01:00", "raid-day",
    "Mega Charizard X Raid Day",
)
TWILIGHT = event(
    "twilight-wishes-2026", "Twilight Wishes",
    "2026-11-24T10:00:00+01:00", "2026-11-30T20:00:00+01:00", "event",
)
MAX_MONDAY = event(
    "max-mondays-2026-11-30", "Dynamax Frigometri am Dyna-Montag",
    "2026-11-30T06:00:00+01:00", "2026-11-30T21:00:00+01:00", "max-mondays",
    "Dynamax Cryogonal during Max Monday",
)
ALL_EVENTS = [GIRATINA, TORNADUS, SPOTLIGHT, CHARIZARD_RAID_DAY, TWILIGHT, MAX_MONDAY]


def match(text, events=ALL_EVENTS, posted=POSTED):
    return event_matcher.match_infographic(text, posted, events, SETTINGS, app.pokemon_species_matches, TZ)


class PokemonRecognitionTest(unittest.TestCase):
    def test_04_multiple_pokemon_in_one_text(self) -> None:
        ids = species_ids("Cresselia und Darkrai in Stufe-5-Raids, Konter: Glurak")
        self.assertEqual(ids, {488, 491, 6})

    def test_05_english_name(self) -> None:
        self.assertEqual(species_ids("Salamence Raid Day"), {373})

    def test_06_german_name(self) -> None:
        self.assertEqual(species_ids("Brutalanda Raid-Tag"), {373})
        self.assertEqual(species_ids("FEEBAS / Barschwa"), {349})

    def test_07_mega_pokemon(self) -> None:
        self.assertEqual(species_ids("🔥 MEGA GLURAK X RAID DAY"), {6})
        self.assertEqual(species_ids("Mega Charizard X"), {6})
        result = match("🔥 MEGA GLURAK X RAID DAY\nNovember 8–9")
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["event_id"], "mega-charizard-x-raid-day")

    def test_08_dynamax_pokemon(self) -> None:
        features = event_matcher.extract_features("Dynamax Barschwa am Dyna-Montag 30. Nov", POSTED, TZ, app.pokemon_species_matches)
        self.assertIn(349, features.pokemon_ids)
        self.assertIn("max-mondays", features.types)
        self.assertNotIn("max-battles", features.types)


class DateParsingTest(unittest.TestCase):
    def dates(self, text, posted=POSTED):
        return event_matcher.extract_dates(text, posted.date())

    def test_formats(self) -> None:
        self.assertEqual(self.dates("4. NOV - 8. NOV")[0], [(date(2026, 11, 4), date(2026, 11, 8))])
        self.assertEqual(self.dates("November 8–9")[0], [(date(2026, 11, 8), date(2026, 11, 9))])
        self.assertEqual(self.dates("8.–9. November")[0], [(date(2026, 11, 8), date(2026, 11, 9))])
        self.assertEqual(self.dates("24.-30.11.")[0], [(date(2026, 11, 24), date(2026, 11, 30))])
        self.assertIn(date(2026, 11, 5), self.dates("5\nNOV")[1])
        self.assertIn(date(2026, 11, 8), self.dates("08.11.2026")[1])
        self.assertIn(date(2026, 11, 8), self.dates("Nov 8th")[1])

    def test_year_rollover(self) -> None:
        posted = datetime(2026, 12, 20, 12, 0, tzinfo=timezone.utc)
        self.assertIn(date(2027, 1, 3), self.dates("3. Januar", posted)[1])
        self.assertEqual(self.dates("30. Dez - 2. Jan", posted)[0], [(date(2026, 12, 30), date(2027, 1, 2))])

    def test_times_and_prices_are_not_dates(self) -> None:
        self.assertEqual(self.dates("18:00 - 19:00 · 5,49€ · 0/14/15 lvl 14.5")[1], set())
        self.assertEqual(event_matcher.extract_times("18:00 - 19:00"), {(18, 0), (19, 0)})
        self.assertIn((18, 0), event_matcher.extract_times("6 p.m."))


class MatchingTest(unittest.TestCase):
    def test_09_matching_date_pokemon_and_type(self) -> None:
        text = "4. NOV - 8. NOV\n#487 GIRATINA\nSchwäche\nSchwierigkeit: 2+ Trainer benötigt\nZacian's Abenteuereffekt"
        result = match(text)
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["event_id"], "giratina-raids")
        self.assertGreaterEqual(result["score"], 80)
        tornadus = next(c for c in result["candidates"] if c["event_id"] == "tornadus-raids")
        self.assertLess(tornadus["score"], result["score"] - 15)

    def test_spotlight_without_readable_name(self) -> None:
        result = match("(Ortszeit)\n5\nNOV\nRampenlichtstunde\n18:00 - 19:00\nTED SA", posted=datetime(2026, 11, 4, 12, tzinfo=timezone.utc))
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["event_id"], "spotlight-teddiursa")

    def test_10_no_clear_match_stays_open(self) -> None:
        result = match("Follow for more G47IX\nwww.g47ix.pro")
        self.assertEqual(result["status"], "pending")
        self.assertIsNone(result["event_id"])

    def test_10b_partial_information_goes_to_review(self) -> None:
        result = match("ZWIELICHT\nWÜNSCHE\n30 NOV\n10:00", posted=datetime(2026, 11, 6, 18, tzinfo=timezone.utc))
        self.assertEqual(result["status"], "manual_review")
        self.assertEqual(result["candidates"][0]["event_id"], "twilight-wishes-2026")

    def test_11_two_similar_events_require_manual_choice(self) -> None:
        # Both raids run on the same days; without a Pokémon name the type and
        # date fit both equally well, so nothing may be assigned automatically.
        result = match("4. NOV - 8. NOV\nRaids\nSchwäche\nTrainer benötigt")
        self.assertNotEqual(result["status"], "matched")
        self.assertIsNone(result["event_id"])
        top = {c["event_id"] for c in result["candidates"][:2]}
        self.assertEqual(top, {"giratina-raids", "tornadus-raids"})

    def test_contradicting_pokemon_lowers_score(self) -> None:
        result = match("4. NOV - 8. NOV GIRATINA Raids")
        scores = {c["event_id"]: c["score"] for c in result["candidates"]}
        self.assertGreater(scores["giratina-raids"], scores.get("tornadus-raids", 0) + 30)

    def test_posted_after_event_is_penalised(self) -> None:
        late = datetime(2026, 12, 15, 12, tzinfo=timezone.utc)
        result = match("4. NOV - 8. NOV GIRATINA Raids Schwäche", posted=late)
        self.assertNotEqual(result["status"], "matched")

    def test_manual_event_can_be_matched(self) -> None:
        manual = event(
            "manual-abc123", "Raid-Treffen Augsburg Rayquaza",
            "2026-11-15T17:00:00+01:00", "2026-11-15T20:00:00+01:00", "manual-raid", manual=True,
        )
        result = match("Rayquaza Raid-Treffen Augsburg\n15. November 17:00 - 20:00", events=ALL_EVENTS + [manual])
        self.assertEqual(result["event_id"], "manual-abc123")


if __name__ == "__main__":
    unittest.main()


class CategoryTest(unittest.TestCase):
    """Texts are excerpts of real OCR output from Galix' graphics."""

    def classify(self, text, posted):
        return event_matcher.classify_infographic(text, posted, TZ)

    def test_weekly_overview(self) -> None:
        text = ("PRAY | ND | GOL, | 28. SEPTEMBER - 4. OKTOBER | MONTAG | DIENSTAG | MITTWOCH | DONNERSTAG | FREITAG | "
                "SAMSTAG | SONNTAG | 28. SEP 29. SEP 30. SEP 1. OKT | Raid- Stunde | ÜBE NAHME")
        result = self.classify(text, datetime(2026, 9, 27, 16, tzinfo=timezone.utc))
        self.assertEqual(result["category"], "weekly")
        self.assertEqual((result["valid_from"], result["valid_until"]), ("2026-09-28", "2026-10-05"))

    def test_weekly_without_readable_dates_uses_posting_week(self) -> None:
        text = "MONTAG DIENSTAG MITTWOCH DONNERSTAG FREITAG SAMSTAG SONNTAG"
        result = self.classify(text, datetime(2026, 11, 7, 12, tzinfo=timezone.utc))  # Saturday
        self.assertEqual((result["valid_from"], result["valid_until"]), ("2026-11-09", "2026-11-16"))

    def test_rocket_takeover(self) -> None:
        text = "910 wP | UB | NAHME | STARTET AM 2. OKT | 309 WP | 335 WP | ERNTE-FESTIVAL | ÜBE"
        result = self.classify(text, datetime(2026, 9, 30, 16, tzinfo=timezone.utc))
        self.assertEqual(result["category"], "rocket")
        self.assertEqual(result["valid_from"], "2026-10-02")

    def test_news_is_only_a_fallback(self) -> None:
        go_pass = self.classify("Beginnt | GO-Pass: | November | NOV | DELUXE | Groudon | Boni für große Meilensteine",
                                datetime(2026, 10, 30, 16, tzinfo=timezone.utc))
        self.assertEqual(go_pass["category"], "event")
        self.assertEqual(go_pass["news_fallback"]["category"], "news")
        updates = self.classify("Spielqualität Updates | ENTDECKER-GADGET ENTWICKELT SICH!", POSTED)
        self.assertEqual(updates["news_fallback"]["category"], "news")
        raid = self.classify("4. NOV - 8. NOV GIRATINA Schwäche Trainer benötigt", POSTED)
        self.assertIsNone(raid["news_fallback"])

    def test_manual_category_info(self) -> None:
        info = event_matcher.category_info("weekly", "irgendein Text", datetime(2026, 11, 4, 12, tzinfo=timezone.utc), TZ)
        self.assertEqual((info["valid_from"], info["valid_until"]), ("2026-11-02", "2026-11-09"))
        self.assertEqual(event_matcher.category_info("news", "", POSTED, TZ)["category"], "news")
