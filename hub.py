"""Additional game data for the "Jetzt aktuell" view.

Loads the current raid bosses, egg pool and field research from ScrapedDuck
(same project as the event feed) and the current weather for the configured
location from Open-Meteo. Everything is cached in memory and on disk, so the
view keeps working with the last known data when a source is unreachable.
Only the Python standard library is used.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

LOGGER = logging.getLogger("scrapedduck-ical.hub")
USER_AGENT = "pogo-kalender/1.8 (+https://github.com/EmreWest/pogo-kalender)"

DEFAULT_HUB_CONFIG: dict[str, Any] = {
    "enabled": True,
    "raids_url": "https://raw.githubusercontent.com/bigfoott/ScrapedDuck/data/raids.json",
    "eggs_url": "https://raw.githubusercontent.com/bigfoott/ScrapedDuck/data/eggs.json",
    "research_url": "https://raw.githubusercontent.com/bigfoott/ScrapedDuck/data/research.json",
    "cache_seconds": 1800,
    "cache_file": "/data/hub-cache.json",
    "weather": {
        "enabled": True,
        "name": "Augsburg",
        "latitude": 48.3705,
        "longitude": 10.8978,
        "cache_seconds": 900,
        "windy_speed_kmh": 25,
        "windy_gust_kmh": 40,
    },
}

TYPE_LABELS = {
    "normal": "Normal", "fire": "Feuer", "water": "Wasser", "grass": "Pflanze", "electric": "Elektro",
    "ice": "Eis", "fighting": "Kampf", "poison": "Gift", "ground": "Boden", "flying": "Flug",
    "psychic": "Psycho", "bug": "Käfer", "rock": "Gestein", "ghost": "Geist", "dragon": "Drache",
    "dark": "Unlicht", "steel": "Stahl", "fairy": "Fee",
}
WEATHER_LABELS = {
    "sunny": "Sonnig", "clear": "Klar", "partly cloudy": "Teilweise bewölkt", "cloudy": "Bewölkt",
    "rainy": "Regen", "snow": "Schnee", "fog": "Nebel", "windy": "Windig",
}
# Pokémon GO weather boosts
WEATHER_BOOSTS = {
    "sunny": ["fire", "grass", "ground"],
    "partly cloudy": ["normal", "rock"],
    "cloudy": ["fairy", "fighting", "poison"],
    "rainy": ["water", "electric", "bug"],
    "snow": ["ice", "steel"],
    "fog": ["dark", "ghost"],
    "windy": ["dragon", "flying", "psychic"],
}

RESEARCH_TYPE_WORDS = {name.capitalize(): label for name, label in TYPE_LABELS.items()}
RESEARCH_PATTERNS: list[tuple[str, str]] = [
    (r"^Catch (\d+) different species of Pokémon$", r"Fange \1 verschiedene Pokémon-Arten"),
    (r"^Catch (\d+) Pokémon with Weather Boost$", r"Fange \1 Pokémon mit Wetterboost"),
    (r"^Catch (\d+) (\w+)-type Pokémon$", r"Fange \1 Pokémon vom Typ {T2}"),
    (r"^Catch an? (\w+)-type Pokémon$", r"Fange ein Pokémon vom Typ {T1}"),
    (r"^Catch (\d+) Pokémon$", r"Fange \1 Pokémon"),
    (r"^Catch a Pokémon$", r"Fange ein Pokémon"),
    (r"^Power up (\w+)-type Pokémon (\d+) times$", r"Verstärke Pokémon vom Typ {T1} \2-mal"),
    (r"^Power up Pokémon (\d+) times$", r"Verstärke Pokémon \1-mal"),
    (r"^Use an? (\w+)-type Charged Attack in a battle$", r"Setze im Kampf eine Lade-Attacke vom Typ {T1} ein"),
    (r"^Hatch an Egg$", r"Brüte ein Ei aus"),
    (r"^Hatch (\d+) Eggs$", r"Brüte \1 Eier aus"),
    (r"^Make (\d+) Excellent Throws in a row$", r"Schaffe \1 fabelhafte Würfe hintereinander"),
    (r"^Make (\d+) Excellent Throws$", r"Schaffe \1 fabelhafte Würfe"),
    (r"^Make an Excellent Throw$", r"Schaffe einen fabelhaften Wurf"),
    (r"^Make (\d+) Great Curveball Throws in a row$", r"Schaffe \1 großartige Curveball-Würfe hintereinander"),
    (r"^Make (\d+) Great Throws in a row$", r"Schaffe \1 großartige Würfe hintereinander"),
    (r"^Make (\d+) Great Throws$", r"Schaffe \1 großartige Würfe"),
    (r"^Make a Great Throw$", r"Schaffe einen großartigen Wurf"),
    (r"^Make (\d+) Nice Throws in a row$", r"Schaffe \1 gute Würfe hintereinander"),
    (r"^Make (\d+) Nice Throws$", r"Schaffe \1 gute Würfe"),
    (r"^Make (\d+) Curveball Throws in a row$", r"Schaffe \1 Curveball-Würfe hintereinander"),
    (r"^Make (\d+) Curveball Throws$", r"Schaffe \1 Curveball-Würfe"),
    (r"^Win a three-star raid or higher$", r"Gewinne einen Raid der Stufe 3 oder höher"),
    (r"^Win a level (\d+) or higher raid$", r"Gewinne einen Raid der Stufe \1 oder höher"),
    (r"^Win a raid$", r"Gewinne einen Raid"),
    (r"^Win (\d+) raids$", r"Gewinne \1 Raids"),
    (r"^Battle in a raid$", r"Kämpfe in einem Raid"),
    (r"^Battle in (\d+) raids$", r"Kämpfe in \1 Raids"),
    (r"^Win a Max Battle$", r"Gewinne einen Dyna-Kampf"),
    (r"^Explore (\d+(?:\.\d+)?) km$", r"Lege \1 km zurück"),
    (r"^Walk (\d+(?:\.\d+)?) km$", r"Lege \1 km zurück"),
    (r"^Spin (\d+) PokéStops or Gyms$", r"Drehe \1 PokéStops oder Arenen"),
    (r"^Spin (\d+) PokéStops$", r"Drehe \1 PokéStops"),
    (r"^Spin a PokéStop or Gym$", r"Drehe einen PokéStop oder eine Arena"),
    (r"^Take a snapshot of a wild Pokémon$", r"Mache einen Schnappschuss von einem wilden Pokémon"),
    (r"^Take a snapshot of your buddy$", r"Mache einen Schnappschuss von deinem Kumpel"),
    (r"^Evolve a Pokémon$", r"Entwickle ein Pokémon"),
    (r"^Evolve (\d+) Pokémon$", r"Entwickle \1 Pokémon"),
    (r"^Transfer (\d+) Pokémon$", r"Verschicke \1 Pokémon"),
    (r"^Earn a Candy walking with your buddy$", r"Verdiene ein Bonbon beim Laufen mit deinem Kumpel"),
    (r"^Earn (\d+) Candies walking with your buddy$", r"Verdiene \1 Bonbons beim Laufen mit deinem Kumpel"),
    (r"^Earn (\d+) hearts with your buddy$", r"Verdiene \1 Herzen mit deinem Kumpel"),
    (r"^Send (\d+) Gifts and add a sticker to each$", r"Verschicke \1 Geschenke mit je einem Sticker"),
    (r"^Send (\d+) Gifts to friends$", r"Verschicke \1 Geschenke an Freunde"),
    (r"^Send a Gift to a friend$", r"Verschicke ein Geschenk an einen Freund"),
    (r"^Trade a Pokémon$", r"Tausche ein Pokémon"),
    (r"^Trade (\d+) Pokémon$", r"Tausche \1 Pokémon"),
    (r"^Defeat a Team GO Rocket Grunt$", r"Besiege einen Rüpel von Team GO Rocket"),
    (r"^Defeat (\d+) Team GO Rocket Grunts$", r"Besiege \1 Rüpel von Team GO Rocket"),
    (r"^Defeat a Team GO Rocket Leader$", r"Besiege einen Anführer von Team GO Rocket"),
    (r"^Use (\d+) berries to help catch Pokémon$", r"Setze \1 Beeren beim Fangen ein"),
    (r"^Use an Incense$", r"Setze ein Rauch-Item ein"),
    (r"^Battle another Trainer$", r"Kämpfe gegen einen anderen Trainer"),
    (r"^Win a Trainer Battle$", r"Gewinne einen Trainerkampf"),
]
NAME_PHRASES = [
    ("Alolan ", "Alola-"), ("Galarian ", "Galar-"), ("Hisuian ", "Hisui-"), ("Paldean ", "Paldea-"),
    (" wearing a Pokémon Halloween costume", " im Halloween-Kostüm"),
    (" wearing a hat and capelet", " mit Hut und Umhang"),
    (" wearing a top hat", " mit Zylinder"),
    (" wearing a bow", " mit Schleife"),
    (" wearing a ", " mit "),
    ("(Female)", "(weiblich)"), ("(Male)", "(männlich)"),
    ("(Altered)", "(Wandelform)"), ("(Origin)", "(Urform)"), ("(Incarnate)", "(Inkarnationsform)"),
    ("(Therian)", "(Tiergeistform)"), ("(White Striped)", "(Weißliniert)"), ("(Blue Striped)", "(Blauliniert)"),
    ("(Red Striped)", "(Rotliniert)"),
]
EGG_KINDS = (("normal", "Normale Eier"), ("adventure", "Abenteuer-Sync"), ("gift", "Aus Geschenken"))


def hub_settings(config: dict[str, Any]) -> dict[str, Any]:
    raw = config.get("hub") if isinstance(config.get("hub"), dict) else {}
    settings = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    for key, value in raw.items():
        if key == "weather" and isinstance(value, dict):
            settings["weather"].update(value)
        else:
            settings[key] = value
    try:
        settings["cache_seconds"] = max(300, int(settings.get("cache_seconds", 1800)))
    except (TypeError, ValueError):
        settings["cache_seconds"] = 1800
    weather = settings["weather"]
    for key, default in (("latitude", 48.3705), ("longitude", 10.8978), ("cache_seconds", 900),
                         ("windy_speed_kmh", 25), ("windy_gust_kmh", 40)):
        try:
            weather[key] = float(weather.get(key, default))
        except (TypeError, ValueError):
            weather[key] = default
    weather["cache_seconds"] = max(300.0, weather["cache_seconds"])
    return settings


def strip_html(value: Any) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", str(value or ""))).split())


def translate_research_task(text: str) -> str:
    for pattern, replacement in RESEARCH_PATTERNS:
        match = re.match(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        result = match.expand(replacement)
        for index, group in enumerate(match.groups(), start=1):
            token = "{T%d}" % index
            if token in result:
                result = result.replace(token, RESEARCH_TYPE_WORDS.get(str(group).capitalize(), str(group)))
        return result
    # Unknown task: keep the English text but translate type names.
    return re.sub(r"\b(\w+)-type\b", lambda m: f"{RESEARCH_TYPE_WORDS.get(m.group(1).capitalize(), m.group(1))}-Typ", text)


def weather_condition(code: int, wind: float, gust: float, settings: dict[str, Any]) -> str:
    """Approximate the in-game weather from WMO weather codes (Open-Meteo)."""
    if code in (45, 48):
        return "fog"
    if code in (71, 73, 75, 77, 85, 86):
        return "snow"
    if 51 <= code <= 67 or 80 <= code <= 82 or code >= 95:
        return "rainy"
    if wind >= settings["windy_speed_kmh"] or gust >= settings["windy_gust_kmh"]:
        return "windy"
    if code == 0 or code == 1:
        return "sunny"
    if code == 2:
        return "partly cloudy"
    return "cloudy"


def condition_payload(condition: str, is_day: bool) -> dict[str, Any]:
    label = WEATHER_LABELS["clear"] if condition == "sunny" and not is_day else WEATHER_LABELS[condition]
    return {
        "key": condition,
        "label": label,
        "boosted": [{"key": t, "label": TYPE_LABELS[t]} for t in WEATHER_BOOSTS[condition]],
    }


class HubService:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, Any] | None = None
        self._fetched = 0.0
        self._weather: dict[str, Any] | None = None
        self._weather_fetched = 0.0
        self._errors: dict[str, str] = {}
        self.fetch: Callable[[str, int], bytes] = self._fetch

    @staticmethod
    def _fetch(url: str, timeout: int) -> bytes:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read(8 * 1024 * 1024)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"Abruf fehlgeschlagen ({urllib.parse.urlsplit(url).netloc}): {exc}") from exc

    def invalidate(self) -> None:
        with self._lock:
            self._fetched = 0.0
            self._weather_fetched = 0.0

    # -- raw data ------------------------------------------------------------
    def _load_disk(self, path: Path) -> dict[str, Any] | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _save_disk(self, path: Path, data: dict[str, Any]) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + ".tmp")
            temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, path)
        except OSError as exc:
            LOGGER.warning("Hub-Cache konnte nicht gespeichert werden: %s", exc)

    def raw_data(self, settings: dict[str, Any], timeout: int) -> dict[str, Any]:
        now = time.monotonic()
        if self._data is not None and now - self._fetched < settings["cache_seconds"]:
            return self._data
        path = Path(str(settings["cache_file"]))
        previous = self._data or self._load_disk(path) or {}
        data: dict[str, Any] = {"fetched_at": previous.get("fetched_at", {})}
        for name in ("raids", "eggs", "research"):
            url = str(settings.get(f"{name}_url") or "")
            try:
                payload = json.loads(self.fetch(url, timeout).decode("utf-8"))
                if not isinstance(payload, list):
                    raise RuntimeError("keine Liste")
                data[name] = [item for item in payload if isinstance(item, dict)]
                data["fetched_at"][name] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                self._errors.pop(name, None)
            except Exception as exc:
                LOGGER.warning("%s konnte nicht geladen werden: %s", name, exc)
                self._errors[name] = str(exc)
                data[name] = list(previous.get(name) or [])
        self._data = data
        # Retry failed sources sooner than the regular interval.
        self._fetched = now if not self._errors else now - settings["cache_seconds"] + 300
        if any(data.get(name) for name in ("raids", "eggs", "research")):
            self._save_disk(path, data)
        return data

    def weather(self, settings: dict[str, Any], timeout: int, tz_name: str) -> dict[str, Any] | None:
        options = settings["weather"]
        if not options.get("enabled", True):
            return None
        now = time.monotonic()
        if self._weather is not None and now - self._weather_fetched < options["cache_seconds"]:
            return self._weather
        query = urllib.parse.urlencode({
            "latitude": f"{options['latitude']:.4f}",
            "longitude": f"{options['longitude']:.4f}",
            "current": "weather_code,wind_speed_10m,wind_gusts_10m,temperature_2m,is_day",
            "hourly": "weather_code,wind_speed_10m,wind_gusts_10m,is_day",
            "forecast_hours": 10,
            "timezone": tz_name,
        })
        try:
            raw = json.loads(self.fetch(f"https://api.open-meteo.com/v1/forecast?{query}", timeout).decode("utf-8"))
            self._weather = self.weather_from_open_meteo(raw, options)
            self._errors.pop("weather", None)
            self._weather_fetched = now
        except Exception as exc:
            LOGGER.warning("Wetter konnte nicht geladen werden: %s", exc)
            self._errors["weather"] = str(exc)
            self._weather_fetched = now - options["cache_seconds"] + 300
            if self._weather is not None:
                self._weather = dict(self._weather, stale=True)
        return self._weather

    @staticmethod
    def weather_from_open_meteo(raw: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
        current = raw["current"]
        condition = weather_condition(int(current["weather_code"]), float(current.get("wind_speed_10m") or 0),
                                      float(current.get("wind_gusts_10m") or 0), options)
        hourly = raw.get("hourly") or {}
        forecast = []
        times = hourly.get("time") or []
        for index, stamp in enumerate(times):
            if stamp[:13] <= str(current.get("time") or "")[:13]:
                continue
            hour_condition = weather_condition(
                int(hourly["weather_code"][index]),
                float(hourly["wind_speed_10m"][index] or 0),
                float(hourly["wind_gusts_10m"][index] or 0),
                options,
            )
            forecast.append({"time": stamp, **condition_payload(hour_condition, bool(hourly.get("is_day", [1] * len(times))[index]))})
        return {
            "location": str(options.get("name") or ""),
            "time": current.get("time"),
            "temperature": current.get("temperature_2m"),
            "windSpeed": current.get("wind_speed_10m"),
            "current": condition_payload(condition, bool(current.get("is_day", 1))),
            "forecast": forecast[:8],
            "source": "Open-Meteo",
        }

    # -- payload -------------------------------------------------------------
    def payload(
        self,
        config: dict[str, Any],
        translate: Callable[[str], str],
        register_image: Callable[[str, str], str | None],
    ) -> dict[str, Any]:
        settings = hub_settings(config)
        if not settings.get("enabled", True):
            return {"enabled": False}
        timeout = int(config.get("request_timeout_seconds", 20))
        with self._lock:
            raw = self.raw_data(settings, timeout)
            weather = self.weather(settings, timeout, str(config.get("timezone") or "Europe/Berlin"))
            errors = dict(self._errors)

        def name_de(value: Any) -> str:
            # Phrases first: the general translation would turn "and" into
            # "und" and break the costume descriptions.
            text = str(value or "")
            for source, target in NAME_PHRASES:
                text = text.replace(source, target)
            return translate(text)

        def pokemon(item: dict[str, Any], cp_key: str = "combatPower") -> dict[str, Any]:
            cp = item.get(cp_key) if isinstance(item.get(cp_key), dict) else {}
            name = name_de(item.get("name"))
            return {
                "name": name,
                "originalName": str(item.get("name") or ""),
                "image": register_image(str(item.get("image") or ""), name),
                "shiny": bool(item.get("canBeShiny")),
                "cp": {"min": cp.get("min"), "max": cp.get("max")} if "min" in cp else None,
            }

        current_weather = (weather or {}).get("current", {}).get("key")
        raids_by_tier: dict[str, dict[str, Any]] = {}
        for item in raw.get("raids") or []:
            tier_raw = str(item.get("tier") or "")
            label, order = self.tier_label(tier_raw)
            tier = raids_by_tier.setdefault(label, {"tier": label, "order": order, "bosses": []})
            cp = item.get("combatPower") if isinstance(item.get("combatPower"), dict) else {}
            weathers = [str(w.get("name") or "") for w in item.get("boostedWeather") or [] if isinstance(w, dict)]
            boss = pokemon(item)
            boss.update({
                "types": [{"key": str(t.get("name") or ""), "label": TYPE_LABELS.get(str(t.get("name") or ""), str(t.get("name") or ""))}
                          for t in item.get("types") or [] if isinstance(t, dict)],
                "cp": (cp.get("normal") or None),
                "cpBoosted": (cp.get("boosted") or None),
                "weather": [{"key": w, "label": WEATHER_LABELS.get(w, w)} for w in weathers],
                "boostedNow": bool(current_weather and current_weather in weathers),
            })
            tier["bosses"].append(boss)
        raids = sorted(raids_by_tier.values(), key=lambda tier: tier["order"])

        eggs_by_distance: dict[str, dict[str, Any]] = {}
        for item in raw.get("eggs") or []:
            distance = str(item.get("eggType") or "?")
            group = eggs_by_distance.setdefault(distance, {"distance": distance, "kinds": {key: [] for key, _ in EGG_KINDS}})
            kind = "adventure" if item.get("isAdventureSync") else ("gift" if item.get("isGiftExchange") else "normal")
            entry = pokemon(item)
            entry.update({"regional": bool(item.get("isRegional")), "rarity": item.get("rarity")})
            group["kinds"][kind].append(entry)

        def distance_key(value: str) -> float:
            match = re.match(r"(\d+(?:\.\d+)?)", value)
            return float(match.group(1)) if match else 999.0

        eggs = []
        for distance in sorted(eggs_by_distance, key=distance_key):
            kinds = eggs_by_distance[distance]["kinds"]
            for key, label in EGG_KINDS:
                if kinds[key]:
                    eggs.append({"distance": distance, "kind": key, "label": f"{distance} · {label}" if key != "normal" else distance,
                                 "pokemon": kinds[key]})

        research = []
        seen_tasks: dict[str, dict[str, Any]] = {}
        for item in raw.get("research") or []:
            task_en = strip_html(item.get("text"))
            if not task_en:
                continue
            entry = seen_tasks.get(task_en)
            if entry is None:
                entry = {"task": translate_research_task(task_en), "taskOriginal": task_en, "rewards": []}
                seen_tasks[task_en] = entry
                research.append(entry)
            names = {reward["originalName"] for reward in entry["rewards"]}
            for reward in item.get("rewards") or []:
                if isinstance(reward, dict) and str(reward.get("name") or "") not in names:
                    names.add(str(reward.get("name") or ""))
                    entry["rewards"].append(pokemon(reward))

        return {
            "enabled": True,
            "raids": raids,
            "eggs": eggs,
            "research": research,
            "weather": weather,
            "fetchedAt": raw.get("fetched_at", {}),
            "errors": errors,
            "source": "LeekDuck.com via ScrapedDuck",
        }

    @staticmethod
    def tier_label(tier: str) -> tuple[str, int]:
        shadow = "shadow" in tier.casefold()
        star = re.search(r"(\d)\s*-?\s*star", tier, flags=re.IGNORECASE)
        if "mega" in tier.casefold():
            return ("Crypto-Mega" if shadow else "Mega"), 2 if not shadow else 12
        if star:
            level = int(star.group(1))
            order = {5: 1, 3: 3, 1: 4}.get(level, 5)
            return (f"Crypto Stufe {level}" if shadow else f"Stufe {level}"), order + (10 if shadow else 0)
        if "elite" in tier.casefold():
            return "Elite-Raids", 6
        return tier or "Raids", 20
