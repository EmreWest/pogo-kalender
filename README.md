# Pokémon-GO-Eventkalender

Selbst gehosteter Pokémon-GO-Eventkalender auf Deutsch. Der Dienst lädt die Events von LeekDuck über [ScrapedDuck](https://github.com/bigfoott/ScrapedDuck), filtert und übersetzt sie und stellt sie als Webkalender und als abonnierbaren iCal-Feed bereit.

Funktionen:
- Webkalender mit Monats-, Wochen- und Listenansicht, mobil mit Agenda und kompakter Monatsansicht
- iCal-Feed für Apple Kalender, Google Kalender, Outlook und andere, auf Wunsch gefiltert nach Eventarten
- deutsche Eventtitel und Pokémon-Namen, Pokémon-Bilder bei Raids und Dyna-Kämpfen
- eigene Events (zum Beispiel lokale Treffen) über eine passwortgeschützte Adminoberfläche
- Ansicht „Aktuell“ mit Raid-Bossen, Eiern, Feldforschung und Wetterboost für einen frei wählbaren Ort
- Merkliste, Countdown-Leiste und Filter nach Eventart, ohne Konto (gespeichert im Browser)
- optional: Infografiken aus einem Discord-Kanal, automatisch den passenden Events zugeordnet

Docker-Image: `ghcr.io/emrewest/pogo-kalender:latest` (amd64 und arm64)

## Inhalt

1. [Aufbau](#aufbau)
2. [Schnellstart](#schnellstart)
3. [Konfiguration](#konfiguration)
4. [Endpunkte](#endpunkte)
5. [Adminbereich und eigene Events](#adminbereich-und-eigene-events)
6. [Eventarten und Filter](#eventarten-und-filter)
7. [Aktuell: Wetter, Raids, Eier, Feldforschung](#aktuell-wetter-raids-eier-feldforschung)
8. [Merkliste, Countdown und Abo-Filter](#merkliste-countdown-und-abo-filter)
9. [Infografiken aus Discord (optional)](#infografiken-aus-discord-optional)
10. [Reverse Proxy](#reverse-proxy)
11. [Backup und Wiederherstellung](#backup-und-wiederherstellung)
12. [Update und Rollback](#update-und-rollback)
13. [Fehlerdiagnose](#fehlerdiagnose)
14. [Entwicklung und Tests](#entwicklung-und-tests)
15. [Datenquellen und Lizenzen](#datenquellen-und-lizenzen)

## Aufbau

```
                 ScrapedDuck (Events, Raids, Eier, Forschung)
                          │
                          ▼
┌──────────────────── scrapedduck-ical (Port 8080) ──────────────────────┐
│ Webkalender, /api/events, iCal-Feed, /admin, Ansicht „Aktuell“          │
│ Zuordnung der Infografiken zu Events                                    │
└──────────────────────────────┬─────────────────────────────────────────┘
                               │  gemeinsames Volume /data
┌──────────────────────────────┴─────────────────────────────────────────┐
│ scrapedduck-ical-discord (optional, kein Port)                         │
│ Discord-Abruf → Bild prüfen und speichern → Texterkennung → Metadaten   │
└────────────────────────────────────────────────────────────────────────┘
```

- Beide Container laufen aus demselben Image und unterscheiden sich nur im Startbefehl.
- Der Webserver funktioniert vollständig ohne den Discord-Worker. Ohne Discord-Zugangsdaten bleibt der Worker im Leerlauf.
- Alle Daten liegen im Volume `/data`: eigene Events, Bild-Caches, Infografiken und deren Metadaten.

| Datei | Aufgabe |
| --- | --- |
| `app.py` | Webserver, Eventverarbeitung, iCal, Admin-API |
| `hub.py` | Raid-Bosse, Eier, Feldforschung und Wetter für „Aktuell“ |
| `locations.py` | Ortssuche nach Postleitzahl und Ortsname |
| `discord_worker.py` | Discord-Collector (eigener Container) |
| `infographic_store.py` | gemeinsame Speicherung mit Dateisperre und atomaren Schreibvorgängen |
| `infographic_manager.py` | Zuordnung, Auslieferung und Admin-Aktionen für Infografiken |
| `event_matcher.py` | Erkennung von Datum, Uhrzeit, Pokémon, Eventtyp und Grafik-Art |
| `ocr.py` | lokale Texterkennung mit Tesseract |

## Schnellstart

Voraussetzung: Docker mit Compose v2.

```bash
mkdir pogo-kalender && cd pogo-kalender
curl -fsSLO https://raw.githubusercontent.com/EmreWest/pogo-kalender/main/docker-compose.yml
curl -fsSLO https://raw.githubusercontent.com/EmreWest/pogo-kalender/main/config.json
curl -fsSL -o .env https://raw.githubusercontent.com/EmreWest/pogo-kalender/main/.env.example
chmod 600 .env
```

In der `.env` mindestens `ADMIN_PASSWORD` setzen. In der `config.json` Kalendername, Zeitzone, Eventarten und den Standardort für das Wetter anpassen. Dann starten:

```bash
docker compose up -d
docker compose logs -f
```

Der Kalender ist danach unter `http://<server>:8080/` erreichbar, der Feed unter `http://<server>:8080/pokemon-go-de.ics` (Pfad einstellbar über `calendar_path`).

Hinweise:
- Docker benennt das Volume nach dem Ordner, in dem die `docker-compose.yml` liegt (`<ordner>_scrapedduck_ical_data`). Bei einem Umzug den Ordnernamen beibehalten oder das Volume mitnehmen, sonst startet der Kalender mit leeren Daten.
- Fehlt die `config.json` beim Start, legt Docker an ihrer Stelle einen leeren Ordner an und der Dienst meldet „Is a directory“. Dann `docker compose down`, den Ordner entfernen, die Datei anlegen und neu starten.
- Selbst bauen statt fertiges Image: in der `docker-compose.yml` bei beiden Diensten `image:` auskommentieren und `build: .` aktivieren.

## Konfiguration

Die `config.json` wird bei jedem Zugriff neu gelesen. Änderungen greifen im Webserver ohne Neustart, den Discord-Worker danach neu starten. Fehlende Werte werden mit Standardwerten ergänzt.

| Schlüssel | Bedeutung |
| --- | --- |
| `calendar_name`, `calendar_description` | Name und Beschreibung des Kalenders |
| `calendar_path` | Pfad des iCal-Feeds, zum Beispiel `/pokemon-go-de.ics` |
| `timezone` | Zeitzone, zum Beispiel `Europe/Berlin` |
| `cache_seconds` | wie lange die ScrapedDuck-Daten zwischengespeichert werden (Standard 1800) |
| `filters` | welche Events im Kalender landen, siehe [Eventarten und Filter](#eventarten-und-filter) |
| `translations` | Übersetzung von Titeln und Pokémon-Namen, eigene Ersetzungen über `manual_phrases` |
| `pokemon_images` | Pokémon-Bilder für Raids und Dyna-Kämpfe |
| `manual_events` | eigene Events und Admin-Login |
| `hub` | Ansicht „Aktuell“ und Wetter-Standardort |
| `infographics` | Discord-Infografiken |

Geheimnisse gehören in die `.env` (Vorlage: `.env.example`):

| Variable | Bedeutung |
| --- | --- |
| `ADMIN_PASSWORD` | Admin-Passwort, hat Vorrang vor `manual_events.admin_password` |
| `DISCORD_BOT_TOKEN`, `DISCORD_GUILD_ID`, `DISCORD_CHANNEL_IDS`, `DISCORD_ALLOWED_AUTHOR_IDS` | nur für die Discord-Infografiken |

## Endpunkte

- `/` – Webkalender
- `/pokemon-go-de.ics` – iCal-Feed, mit `?groups=raids,community` auf Eventarten begrenzbar
- `/api/events` – Events und Metadaten für die Webansicht
- `/api/hub` – Raid-Bosse, Eier, Feldforschung und Wetter am Standardort
- `/api/locations?q=…` – Ortssuche nach Postleitzahl und/oder Ortsname
- `/api/weather?lat=…&lon=…&name=…` – Wetter für einen gewählten Ort
- `/infographic/<id>` – Infografik-Datei (öffentlich nur, wenn sie zugeordnet ist)
- `/status` – Statusseite, `/health` – JSON-Status für Monitoring
- `/admin` – Verwaltung von eigenen Events und Infografiken

## Adminbereich und eigene Events

Der Login unter `/admin` braucht nur ein Passwort: `ADMIN_PASSWORD` aus der `.env` oder `manual_events.admin_password` aus der `config.json`. Solange das mitgelieferte Platzhalter-Passwort aktiv ist, zeigt die Seite eine Warnung.

Sicherheit:
- Sitzungs-Cookie mit `HttpOnly`, `SameSite=Strict` und standardmäßig `Secure`, deshalb `/admin` über HTTPS öffnen. Nur für einen rein lokalen Test über HTTP kann `manual_events.secure_cookie` vorübergehend auf `false` gesetzt werden.
- CSRF-Token für alle schreibenden Anfragen, konstanter Passwortvergleich, Begrenzung fehlgeschlagener Logins, zeitlich begrenzte Sitzungen im Arbeitsspeicher (ein Neustart beendet sie), `Cache-Control: no-store`

Eigene Events haben Titel, Eventtyp, ganztägig oder Start/Ende, Beschreibung, Ort, Link und optional Pokémon für Bilder (deutsche und englische Namen, auch mit Zusätzen wie „Mega“ oder „Dynamax“). Vordefinierte Typen sind `manual-event`, `manual-meetup`, `manual-raid` und `manual-reminder`, eigene Typen aus Kleinbuchstaben, Zahlen und Bindestrichen sind möglich.

Campfire-Meetups lassen sich im Feld „Aus Campfire übernehmen“ importieren:
- Link: Teilen-Link (`https://cmpf.re/…`) oder den ganzen Teilen-Text einfügen. Der Server ruft die öffentliche Meetup-Seite ab und übernimmt Titel, Beschreibung und Link. Datum, Uhrzeit und Ort stehen dort nicht.
- Screenshot: Ein Screenshot des Meetups aus der Campfire-App (Deutsch oder Englisch) liefert per Texterkennung Datum, Uhrzeit und Ort, ohne Link auch Titel und Beschreibung. Am Desktop kann der Screenshot auch mit Strg+V bzw. Cmd+V in das Linkfeld eingefügt werden. Das Bild wird nur ausgelesen und nicht gespeichert.
- Am besten beides zusammen übergeben. Die Werte landen im Formular und werden vor dem Speichern geprüft. Ist das Meetup schon eingetragen, weist der Admin darauf hin. Im Kalender erscheint der Link als „In Campfire öffnen“.

Gespeichert wird atomar in `/data/manual-events.json`. Bei ganztägigen Events ist das Ende wie in iCal üblich exklusiv: Ein Event nur am 22. August steht als `"start": "2026-08-22", "end": "2026-08-23"` in der Datei. Ein Beispiel liegt in `manual-events.example.json`.

## Eventarten und Filter

Welche ScrapedDuck-Eventarten im Kalender erscheinen, steuert `filters` in der `config.json`:

| Schlüssel | Bedeutung |
| --- | --- |
| `include_types` | nur diese Eventarten (leer = alle), zum Beispiel `raid-battles`, `community-day`, `wild-area`, `timed-research`, `go-pass`, `season` |
| `exclude_types` | diese Eventarten ausblenden |
| `global_only_types` | bei diesen Arten nur die globale Ausgabe zeigen (Standard: `wild-area`, `pokemon-go-tour`, `pokemon-go-fest`), lokale Vor-Ort-Ausgaben werden ausgeblendet |
| `include_keywords`, `exclude_keywords` | Stichwörter in Titel oder Eventtyp |
| `past_days`, `future_days` | Zeitraum um das heutige Datum |

Für die Filter-Chips und das gefilterte Abo werden die Eventarten zu Gruppen zusammengefasst: `raids`, `dyna`, `spotlight`, `community`, `big` (GO Fest, GO Tour, GO Naturzone …), `research`, `rocket`, `events` (alles andere) und `local` (eigene Events aus dem Admin).

## Aktuell: Wetter, Raids, Eier, Feldforschung

Die Ansicht „Aktuell“ (mobil dritter Reiter, am Desktop neben Monat, Woche und Liste) zeigt:
- Wetter mit den im Spiel verstärkten Typen und einer Vorschau für die nächsten Stunden
- Raid-Bosse nach Stufe mit Typen, WP, Wetterboost-WP und Shiny-Hinweis. Bosse, die beim aktuellen Wetter verstärkt sind, werden hervorgehoben.
- Eier nach Distanz, Abenteuer-Sync- und Geschenk-Eier getrennt
- Feldforschung mit deutschen Aufgabentexten und Suche nach Aufgabe oder Pokémon. Unbekannte Formulierungen bleiben englisch, nur die Typnamen werden übersetzt.

Raids, Eier und Forschung kommen von ScrapedDuck und werden alle 30 Minuten aktualisiert. Ist eine Quelle nicht erreichbar, zeigt die Seite den zuletzt bekannten Stand aus `/data/hub-cache.json`.

Wetter:
- Daten von [Open-Meteo](https://open-meteo.com/) (kostenlos, ohne API-Schlüssel), alle 15 Minuten aktualisiert
- Die Einordnung als Pokémon-GO-Wetter ist eine Näherung, weil das Spiel eigene Wetterdaten nutzt. „Windig“ gilt ab `windy_speed_kmh` oder Böen ab `windy_gust_kmh`.
- Der Standardort steht in der `config.json`. Besucher können über „Ort ändern“ einen eigenen Ort wählen: Postleitzahl und/oder Ortsnamen eingeben und aus der Trefferliste auswählen. Der Gerätestandort wird nicht abgefragt, die Wahl speichert nur der Browser.
- Deutsche Postleitzahlen und Orte werden offline in `plz-de.json` gesucht, die Eingabe verlässt den Server dabei nicht. Orte außerhalb Deutschlands sucht der Server bei Open-Meteo.
- Wetter wird pro Ort 15 Minuten zwischengespeichert. Anfragen, die externe Abrufe auslösen, sind pro Besucher begrenzt.

```json
"hub": {
  "enabled": true,
  "cache_seconds": 1800,
  "weather": {
    "enabled": true,
    "name": "Berlin",
    "latitude": 52.52,
    "longitude": 13.405,
    "cache_seconds": 900,
    "windy_speed_kmh": 25,
    "windy_gust_kmh": 40
  }
}
```

## Merkliste, Countdown und Abo-Filter

- Filter-Chips über dem Kalender blenden Eventarten ein und aus. Die Auswahl merkt sich der Browser.
- Der Stern in der Detailansicht setzt ein Event auf die Merkliste. Der Chip „Merkliste“ zeigt nur gemerkte Events.
- Die Countdown-Leiste zeigt laufende und in den nächsten sieben Tagen beginnende kurze Events (Raid-Stunde, Rampenlicht, Community Day, Dyna-Montag, große Events, eigene Treffen) sowie alle Events der Merkliste, jeweils mit Restzeit.
- „Abonnieren“ öffnet eine Auswahl der Eventarten und erzeugt daraus eine Abo-Adresse, zum Beispiel `…/pokemon-go-de.ics?groups=raids,community`. Die Adresse ohne Parameter liefert alle Events.

## Infografiken aus Discord (optional)

Der Discord-Worker übernimmt Infografiken aus einem Discord-Kanal, speichert sie lokal und zeigt sie beim passenden Event an, jeweils mit Quellenangabe. Er ist auf die Infografiken von G47IX abgestimmt, funktioniert aber mit jedem Kanal, der Grafiken zu Pokémon-GO-Events veröffentlicht.

Wichtig: Fremde Grafiken nur mit Erlaubnis der Erstellerin oder des Erstellers einbinden und die Quellenangabe unter `infographics.attribution` entsprechend setzen.

### Variante wählen

- Der Bot wird in den Server eingeladen, in dem die Grafiken erscheinen. Dafür braucht jemand mit dem Recht „Server verwalten“ in diesem Server.
- Oder: Ist der Kanal ein Ankündigungskanal, folgst du ihm mit „Folgen“ in einen Kanal deines eigenen Servers. Discord überträgt jede neue Nachricht samt Bildern dorthin, und der Bot liest nur in deinem Server mit. Dafür brauchst du in deinem Server das Recht „Webhooks verwalten“.

### Discord Application und Bot anlegen

1. https://discord.com/developers/applications öffnen, „New Application“ anlegen.
2. Unter „Bot“:
   - „Reset Token“ klicken und den Token in die `.env` übernehmen. Er wird nur einmal angezeigt.
   - „Public Bot“ ausschalten.
   - Unter „Privileged Gateway Intents“ nur „Message Content Intent“ einschalten. Ohne diesen Intent liefert Discord Inhalt, Anhänge und Embeds fremder Nachrichten leer aus, auch über die REST-API. Bots in weniger als 100 Servern brauchen dafür keine Verifizierung.
3. Unter „OAuth2“ → „URL Generator“ den Scope `bot` und nur die Berechtigungen „View Channels“ und „Read Message History“ wählen, die URL öffnen und den Server auswählen.
4. Optional: In den Kanalberechtigungen des Bots alles außer diesen beiden Rechten verbieten.

Der Bot sendet keine Nachrichten und reagiert auf nichts. Er baut keine Gateway-Verbindung auf, sondern fragt den Kanal regelmäßig über die REST-API ab.

### IDs ermitteln und eintragen

In Discord unter Einstellungen → Erweitert den Entwicklermodus einschalten, dann per Rechtsklick „ID kopieren“:
- Server-ID → `DISCORD_GUILD_ID` (der Server, in dem der Bot mitliest)
- Kanal-ID → `DISCORD_CHANNEL_IDS` (mehrere mit Komma trennen)
- Autor-ID → `DISCORD_ALLOWED_AUTHOR_IDS` (optional, empfohlen). Bei gefolgten Kanälen ist das die ID des Follow-Webhooks. Nach der ersten übertragenen Nachricht zeigt dieser Befehl die IDs an:

  ```bash
  docker compose run --rm scrapedduck-ical-discord python /app/discord_worker.py --show-authors
  ```

  Ohne diesen Wert verarbeitet der Worker jede Nachricht im Kanal.

Der Worker prüft beim Start, ob der Kanal zur angegebenen Server-ID gehört. Der Token steht nie in der `config.json`, in HTML, im Repository oder im Log.

### Betrieb

```bash
docker compose up -d                              # nach Änderungen an der .env (Container wird neu erstellt)
docker compose restart scrapedduck-ical-discord   # nach Änderungen an der config.json
docker compose logs -f scrapedduck-ical-discord
docker compose run --rm scrapedduck-ical-discord python /app/discord_worker.py --once   # ein einzelner Abruf
```

- Historischer Import: Beim ersten Start eines Kanals werden die letzten `historical_import.message_limit` Nachrichten importiert (Standard 100). Mit `"enabled": false` geht es ab der neuesten Nachricht weiter. Danach holt der Worker nur neue Nachrichten ab der zuletzt gesehenen ID und setzt nach einem Neustart dort fort. Um den Import zu wiederholen, den Worker stoppen, `/data/discord-worker-state.json` löschen und neu starten. Vorhandene Grafiken werden nicht doppelt angelegt.
- Downloads nur per HTTPS von `cdn.discordapp.com` und `media.discordapp.net`. Geprüft werden Endung, MIME-Typ, Größe (`max_file_size_mb`) und das echte Dateiformat. Erlaubt sind JPEG, PNG und WebP.
- Duplikate werden über die ID und die SHA-256-Prüfsumme erkannt. Bearbeitete Nachrichten werden über die zuletzt geprüften Nachrichten (`recheck_recent_messages`) erkannt und neu bewertet. Gelöschte Discord-Nachrichten erkennt der Worker nicht, solche Grafiken im Admin löschen.
- Ablage: Originalbilder unter `/data/infographics/JJJJ/MM/`, Metadaten in `/data/infographic-events.json`, Worker-Status in `/data/discord-worker-state.json`

### Zuordnung zu Events

Ausgewertet werden der Nachrichtentext und eine lokale Texterkennung (Tesseract, Deutsch und Englisch, zwei Durchgänge für helle und dunkle Schrift). Daraus werden Datum und Zeitraum, Uhrzeiten, Pokémon (deutsche und englische Namen), Eventtyp und Titelwörter gelesen und für jedes Event im Kalender Punkte vergeben:

| Kriterium | Punkte |
| --- | --- |
| Datumsbereich exakt bzw. Datum eines Eintagesevents | 50 |
| Start- und Enddatum einzeln gefunden | 45 |
| Start- oder Enddatum | 35 |
| Datum liegt im Eventzeitraum | 15 |
| Uhrzeiten passen | 6–10 |
| Pokémon passt / widerspricht | 30 / −20 |
| Eventtyp passt / verwandt / widerspricht | 20 / 8 / −10 |
| Titelwörter (deutscher und englischer Titel getrennt bewertet) | bis 20 |
| Post bis 30 Tage vor Eventende / nach Eventende | 10 / −40 |

- Ab `auto_match_threshold` (Standard 80) wird automatisch zugeordnet, sofern ein Datum erkannt wurde und der Vorsprung vor dem zweitbesten Event mindestens `min_margin` (Standard 15) beträgt.
- Ab `manual_review_threshold` (Standard 50) landet die Grafik zur Prüfung im Admin, mit Vorschlägen.
- Darunter bleibt sie offen und wird bei jeder Kalenderaktualisierung neu bewertet, zum Beispiel sobald das Event bei ScrapedDuck erscheint.

Automatische Zuordnungen bleiben stabil, manuelle Entscheidungen werden nie automatisch überschrieben.

### Grafik-Arten

| Art | Erkennung | Anzeige | Gültigkeit |
| --- | --- | --- | --- |
| Wochenübersicht | mindestens fünf Wochentage oder Stichwort „Weekly/Wochenübersicht“ | Leiste über dem Kalender, mobil auch beim gewählten Tag | im erkannten Wochenzeitraum, ab `weekly_lead_days` vorher als „Nächste Woche“ |
| Team GO Rocket | Übernahme/Takeover, Rocket, Giovanni, Sierra, Cliff, Arlo, Rüpel, Anführer | Leiste und Kachel bei den hilfreichen Links | die neueste Grafik, höchstens `rocket_max_age_days` |
| Neuigkeit | GO-Pass, Update, Spielqualität, Saison, Ankündigung, nur wenn kein Event passt | Leiste | `news_days` ab Post, höchstens `news_max_items` |
| Event | alles andere | Detailansicht des zugeordneten Events | wie das Event |

Als Neuigkeit eingeordnete Grafiken werden weiter geprüft und wandern zum Event, sobald ein passendes erscheint.

### Verwaltung im Admin

Im Bereich „Infografiken“ unter `/admin` stehen zu jeder Grafik Vorschau, Status (Offen, Zugeordnet, Zu prüfen, Abgelehnt, Fehler), Punkte, Begründung, erkannte Daten, Nachrichtentext und Texterkennung. Möglich sind:
- Art ändern (Event, Wochenübersicht, Team GO Rocket, Neuigkeit)
- Vorschlag mit einem Klick zuordnen oder ein beliebiges Event wählen, auch eigene Events
- Zuordnung entfernen, ablehnen, automatisch neu bewerten
- löschen (die Grafik wird danach nicht erneut importiert) und aufräumen (alle abgelehnten oder alle älter als 120 Tage)

### Konfiguration

```json
"infographics": {
  "enabled": true,
  "max_file_size_mb": 15,
  "allowed_extensions": [".jpg", ".jpeg", ".png", ".webp"],
  "poll_interval_seconds": 300,
  "recheck_recent_messages": 25,
  "historical_import": { "enabled": true, "message_limit": 100 },
  "ocr": { "enabled": true, "languages": "deu+eng", "timeout_seconds": 90 },
  "matching": { "auto_match_threshold": 80, "manual_review_threshold": 50, "min_margin": 15 },
  "featured": { "weekly_lead_days": 3, "rocket_max_age_days": 60, "news_days": 14, "news_max_items": 6 },
  "attribution": {
    "creator": "Name der Erstellerin oder des Erstellers",
    "text": "Infografik von … – mit freundlicher Genehmigung",
    "source_url": "https://…"
  }
}
```

`"enabled": false` schaltet die Funktion ab, `"ocr": {"enabled": false}` nur die Texterkennung. Weitere Optionen: `allowed_download_hosts`, `include_embed_images`, `download_timeout_seconds`, `matching.max_candidates`, `matching.title_glossary`.

## Reverse Proxy

Der Webserver lauscht auf Port 8080 und braucht keinen eigenen Reverse Proxy. Für den öffentlichen Betrieb empfiehlt sich HTTPS über einen vorhandenen Proxy (zum Beispiel Nginx, Caddy, Traefik oder HAProxy). Den Host komplett an Port 8080 weiterleiten. Wer nur bestimmte Pfade freigibt, braucht:

```text
/
/pokemon-go-de.ics        (bzw. der eingestellte calendar_path)
/api/events
/api/hub
/api/locations
/api/weather
/status
/health
/pokemon-image/*
/infographic/*
/favicon.svg /favicon.ico /favicon-32x32.png /apple-touch-icon.png /icon-192.png /icon-512.png /site.webmanifest
/admin
/api/admin/*
```

Die Client-IP für Login-Begrenzung und Abruflimits liest der Dienst aus `CF-Connecting-IP`, `X-Forwarded-For` oder `X-Real-IP`.

Ausgehend braucht der Webserver HTTPS-Zugriff auf `raw.githubusercontent.com`, `cdn.leekduck.com`, `api.open-meteo.com` und `geocoding-api.open-meteo.com` (für den Campfire-Import zusätzlich `cmpf.re` und `campfire.scopely.com`), der Discord-Worker auf `discord.com` und `cdn.discordapp.com`.

## Backup und Wiederherstellung

Alle Daten liegen im Volume `<ordner>_scrapedduck_ical_data`. Sichern:

```bash
docker run --rm -v <ordner>_scrapedduck_ical_data:/data:ro -v "$PWD":/backup alpine \
  tar czf /backup/pogo-kalender-$(date +%F).tar.gz -C /data .
```

Zusätzlich `.env` und `config.json` sichern. Wiederherstellen:

```bash
docker compose down
docker run --rm -v <ordner>_scrapedduck_ical_data:/data -v "$PWD":/backup alpine \
  sh -c "cd /data && tar xzf /backup/pogo-kalender-JJJJ-MM-TT.tar.gz"
docker compose up -d
```

## Update und Rollback

```bash
docker compose pull
docker compose up -d
```

Neue Einstellungen erscheinen in der `config.json` im Repository. Fehlen sie in der eigenen Datei, gelten die Standardwerte.

Jedes Image trägt zusätzlich den Commit als Tag (`ghcr.io/emrewest/pogo-kalender:sha-<commit>`). Für ein Rollback in der `docker-compose.yml` bei beiden Diensten `:latest` durch den gewünschten Tag ersetzen und `docker compose up -d` ausführen. Vorher ein Backup machen.

## Fehlerdiagnose

| Symptom | Ursache / Lösung |
| --- | --- |
| „Is a directory: '/config/config.json'“ | Die `config.json` fehlte beim Start, siehe [Schnellstart](#schnellstart) |
| Ein Event fehlt im Kalender | Eventart nicht in `filters.include_types`, ausgeschlossen über `exclude_types`/`exclude_keywords` oder außerhalb von `past_days`/`future_days` |
| Admin-Login klappt nicht über HTTP | Secure-Cookie, `/admin` über HTTPS öffnen |
| Worker-Log: „Fehlende Discord-Konfiguration“ | `.env` fehlt, liegt nicht im Compose-Ordner oder ein Wert ist leer. Danach `docker compose up -d`. |
| `Discord-API 401` | Token falsch oder zurückgesetzt |
| `Discord-API 403 Missing Access` | Bot fehlt im Kanal oder „View Channel“ bzw. „Read Message History“ fehlt |
| `Discord-API 404` | Kanal-ID falsch oder Bot nicht im Server |
| „gehört nicht zum konfigurierten Server“ | `DISCORD_GUILD_ID` passt nicht zum Kanal |
| Nachrichten kommen an, aber keine Bilder | Message Content Intent nicht aktiv, oder `DISCORD_ALLOWED_AUTHOR_IDS` enthält nicht die richtige ID (`--show-authors`) |
| Grafik bleibt „Offen“ | Passendes Event noch nicht im Kalender, oder Eventart herausgefiltert. Sonst im Admin zuordnen. |
| Wetter fehlt | Open-Meteo nicht erreichbar oder `hub.weather.enabled` ist `false` |

Hilfreiche Befehle:

```bash
docker compose ps
docker compose logs --tail 200 scrapedduck-ical
docker compose exec scrapedduck-ical-discord cat /data/discord-worker-state.json
curl -s http://localhost:8080/health | python3 -m json.tool
```

## Entwicklung und Tests

Die Tests brauchen keine echten Zugangsdaten. Discord wird mit Testnachrichten simuliert, der OCR-Test läuft nur, wenn Tesseract installiert ist.

```bash
pip install -r requirements.txt
python -m unittest discover -s tests -t . -v
```

GitHub Actions führt bei jedem Push die Tests aus, baut das Image, prüft Webserver, iCal-Feed und den Leerlauf des Workers im Container, wiederholt die Tests im fertigen Image und veröffentlicht es in der GitHub Container Registry.

## Datenquellen und Lizenzen

- Eventdaten, Raids, Eier und Feldforschung: [LeekDuck](https://leekduck.com/) über [ScrapedDuck](https://github.com/bigfoott/ScrapedDuck)
- Wetter und Ortssuche außerhalb Deutschlands: [Open-Meteo](https://open-meteo.com/) (CC BY 4.0)
- Deutsche Postleitzahlen: [GeoNames](https://www.geonames.org/) (CC BY 4.0)
- Pokémon-Namen und Bilder: PokeAPI und LeekDuck
- Infografiken: Rechte bei den jeweiligen Erstellerinnen und Erstellern, Einbindung nur mit deren Erlaubnis

Pokémon und Pokémon GO sind Marken ihrer jeweiligen Inhaber. Dieses Projekt steht in keiner Verbindung zu Niantic, Nintendo, The Pokémon Company oder LeekDuck.
