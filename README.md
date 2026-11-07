# Pokémon-GO-Eventkalender (ScrapedDuck iCal)

Selbst gehosteter Pokémon-GO-Eventkalender für die Community in Augsburg. Der Dienst lädt die Events von LeekDuck über ScrapedDuck, filtert und übersetzt sie und stellt sie als Webkalender und als abonnierbaren iCal-Feed bereit. Eigene Events lassen sich über eine passwortgeschützte Adminoberfläche pflegen.

Seit Version 1.7.0 kommen die Infografiken von G47IX (Galix) dazu. Sie werden automatisch aus Discord übernommen und beim passenden Event in der Detailansicht angezeigt, mit Quellenangabe. Galix hat der Einbindung zugestimmt.

- Kalender: https://pogo-kalender.emrebati.de/
- iCal-Feed: https://pogo-kalender.emrebati.de/pokemon-go-de.ics
- Docker-Image: `ghcr.io/emrewest/pogo-kalender:latest` (amd64 und arm64)

## Inhalt

1. [Aufbau](#aufbau)
2. [Installation und Update](#installation-und-update)
3. [Öffentliche Endpunkte](#öffentliche-endpunkte)
4. [Admin-Passwort](#admin-passwort)
5. [Manuelle Events](#manuelle-events)
6. [G47IX-Infografiken über Discord](#g47ix-infografiken-über-discord)
7. [Matching: wie Grafiken Events zugeordnet werden](#matching-wie-grafiken-events-zugeordnet-werden)
8. [Manuelle Zuordnung im Adminbereich](#manuelle-zuordnung-im-adminbereich)
9. [Konfiguration](#konfiguration)
10. [HAProxy und Cloudflare](#haproxy-und-cloudflare)
11. [Backup und Wiederherstellung](#backup-und-wiederherstellung)
12. [Update und Rollback](#update-und-rollback)
13. [Fehlerdiagnose](#fehlerdiagnose)
14. [Entwicklung und Tests](#entwicklung-und-tests)

## Aufbau

```
                 ScrapedDuck (events.json)
                          │
                          ▼
┌───────────────────── scrapedduck-ical (Port 8080) ─────────────────────┐
│ Event-Verarbeitung → Webkalender, /api/events, iCal-Feed, /admin       │
│ Matching der Infografiken gegen die aktuellen Events                    │
└──────────────────────────────┬─────────────────────────────────────────┘
                               │  gemeinsames Volume /data
┌──────────────────────────────┴─────────────────────────────────────────┐
│ scrapedduck-ical-discord (kein Port)                                   │
│ Discord-REST-Abruf → Bild laden → prüfen → speichern → OCR → Metadaten  │
└────────────────────────────────────────────────────────────────────────┘
```

Beide Container laufen aus demselben Image und unterscheiden sich nur im Startbefehl.

- Der Webserver läuft unabhängig vom Worker. Ist Discord nicht erreichbar, der Worker gestoppt oder nicht eingerichtet, funktionieren Kalender, iCal-Feed und Admin unverändert weiter.
- Der Worker sammelt nur ein. Die Zuordnung zu Events macht der Webserver, denn nur er kennt die aktuellen Events. Grafiken, die vor dem passenden ScrapedDuck-Event erscheinen, bleiben offen und werden bei jeder Kalenderaktualisierung neu bewertet.
- ScrapedDuck bleibt die einzige Quelle für Eventdaten. Discord liefert nur Bilder.

| Datei | Aufgabe |
| --- | --- |
| `app.py` | Webserver, Eventverarbeitung, iCal, Admin-API (bestehend, erweitert) |
| `discord_worker.py` | Discord-Collector (eigener Container) |
| `infographic_store.py` | gemeinsame Speicherung mit Dateisperre und atomaren Schreibvorgängen |
| `infographic_manager.py` | Matching-Lauf, Auslieferung, Admin-Aktionen im Webserver |
| `event_matcher.py` | Erkennung von Datum, Uhrzeit, Pokémon und Eventtyp, Punktebewertung |
| `ocr.py` | lokale Texterkennung mit Tesseract |

## Installation und Update

Voraussetzung: Docker mit Compose v2 auf dem Ubuntu-Server.

### Umstieg vom bisherigen Stand (v10 / 1.6.0)

1. Im bisherigen Projektordner bleiben, also im Ordner, in dem die alte `docker-compose.yml` liegt. Docker benennt das Volume nach diesem Ordner (`<ordner>_scrapedduck_ical_data`). Nur im selben Ordner werden manuelle Events, Bilder und Caches weiterverwendet. Den Namen prüfen:

   ```bash
   docker volume ls | grep scrapedduck_ical_data
   ```

2. Vorher sichern (siehe [Backup](#backup-und-wiederherstellung)).
3. `docker-compose.yml`, `.env.example` und die neue `config.json` aus diesem Repository in den Ordner kopieren. In die neue `config.json` die eigenen Anpassungen übernehmen, zum Beispiel Filter. Neu ist nur der Abschnitt `infographics`.
4. `.env` anlegen und das Admin-Passwort eintragen (siehe [Admin-Passwort](#admin-passwort)):

   ```bash
   cp .env.example .env
   chmod 600 .env
   nano .env
   ```

5. Starten:

   ```bash
   docker compose pull
   docker compose up -d
   docker compose logs -f
   ```

Ohne Discord-Werte in der `.env` startet der Worker trotzdem. Er schreibt dann „Fehlende Discord-Konfiguration“ ins Log und wartet im Leerlauf. Der Kalender ist davon nicht betroffen.

### Selbst bauen statt fertiges Image

In der `docker-compose.yml` bei beiden Diensten `image:` auskommentieren und `build: .` aktivieren, dann:

```bash
docker compose build --no-cache
docker compose up -d
```

## Öffentliche Endpunkte

- `/` – Webkalender mit Monats-, Wochen- und Listenansicht
- `/pokemon-go-de.ics` – abonnierbarer iCal-Feed (unverändert, ohne Infografiken)
- `/api/events` – Events und Metadaten für die Webansicht. Zugeordnete Infografiken stehen im Feld `infographics`.
- `/infographic/<id>` – Infografik-Datei. Öffentlich nur, wenn sie einem Event zugeordnet ist.
- `/status` – visuelle Statusseite, jetzt mit Infografik- und Worker-Status
- `/health` – maschinenlesbarer JSON-Status
- `/admin` – Verwaltung von manuellen Events und Infografiken

## Admin-Passwort

Das Passwort steht vorzugsweise in der `.env`:

```bash
ADMIN_PASSWORD=ein-langes-eigenes-passwort
```

`ADMIN_PASSWORD` hat Vorrang vor `manual_events.admin_password` in der `config.json`. In der `config.json` aus dem Repository steht nur der Platzhalter `CHANGE-ME-NOW-2026!`. Solange er aktiv ist, warnt die Adminseite.

Die Sitzung verwendet ein Cookie mit `HttpOnly`, `SameSite=Strict` und standardmäßig `Secure`. Öffne `/admin` deshalb über HTTPS. Nur für einen rein lokalen Test über HTTP kann `manual_events.secure_cookie` vorübergehend auf `false` gesetzt werden.

Weitere Schutzmaßnahmen:
- konstanter Passwortvergleich
- CSRF-Token für alle schreibenden Anfragen
- Begrenzung fehlgeschlagener Logins
- zeitlich begrenzte In-Memory-Sitzungen
- `Cache-Control: no-store`

Ein Container-Neustart beendet alle Sitzungen.

## Manuelle Events

Nach dem Login können Titel, Eventtyp, ganztägig oder Start/Ende, Beschreibung, Ort, Link und Pokémon für Bilder gepflegt werden. Deutsche und englische Namen werden erkannt, ebenso Zusätze wie `Mega Glurak X` oder `Dynamax Barschwa`.

Vordefinierte Typen sind `manual-event`, `manual-meetup`, `manual-raid` und `manual-reminder`. Eigene Typen aus Kleinbuchstaben, Zahlen und Bindestrichen sind möglich.

Gespeichert wird atomar in `/data/manual-events.json`. Bei ganztägigen Events ist das gespeicherte Ende exklusiv, wie in iCal üblich: Ein Event nur am 22. August steht als `"start": "2026-08-22", "end": "2026-08-23"` in der Datei.

## G47IX-Infografiken über Discord

Galix lädt keine fremden Bots in seinen Server ein. Sein Infografik-Kanal ist aber ein Ankündigungskanal. Den abonnierst du mit „Folgen“ in einen Kanal deines eigenen Servers, und nur dort liest der Bot mit. Discord überträgt jede neue Galix-Nachricht samt Bildern automatisch in deinen Kanal.

### 1. Eigenen Server und Kanal vorbereiten

1. In Discord einen eigenen Server anlegen oder einen vorhandenen nutzen, zum Beispiel mit einem Kanal `#g47ix-infografiken`.
2. Im Galix-Server den Infografik-Ankündigungskanal öffnen und oben auf „Folgen“ klicken. Als Ziel deinen Server und den neuen Kanal wählen. Dafür brauchst du dort das Recht „Webhooks verwalten“.

### 2. Discord Application und Bot anlegen

1. https://discord.com/developers/applications öffnen, dann „New Application“, Name zum Beispiel `PoGo-Kalender`.
2. Links auf „Bot“ gehen:
   - „Reset Token“ klicken und den Token kopieren. Er wird nur einmal angezeigt und kommt ausschließlich in die `.env`.
   - „Public Bot“ ausschalten, damit niemand sonst ihn einladen kann.
   - Unter „Privileged Gateway Intents“ nur „Message Content Intent“ einschalten. Ohne diesen Intent liefert Discord Inhalt, Anhänge und Embeds fremder Nachrichten leer aus, auch über die REST-API. „Presence“ und „Server Members“ bleiben aus. Bots in weniger als 100 Servern brauchen dafür keine Verifizierung.

Der Worker baut keine Gateway-Verbindung auf. Er fragt den Kanal alle paar Minuten über die REST-API ab. Das reicht für Infografiken, übersteht Neustarts ohne Lücken und bleibt weit unter den Rate-Limits.

### 3. Bot in den eigenen Server einladen

1. „OAuth2“ → „URL Generator“ öffnen, Scope `bot` wählen.
2. Bei den Bot-Berechtigungen nur „View Channels“ und „Read Message History“ anhaken. Nicht wählen: Nachrichten senden oder verwalten, Rollen, Mitglieder, Server verwalten, Moderation.
3. Die erzeugte URL öffnen und deinen eigenen Server auswählen.
4. Optional, aber empfohlen: In den Kanalberechtigungen des Bots alles außer den beiden Rechten verbieten. In anderen Kanälen braucht er keinen Zugriff.

Der Bot sendet nie Nachrichten und reagiert auf nichts.

### 4. IDs ermitteln

In Discord unter Einstellungen → Erweitert den Entwicklermodus einschalten. Danach:
- Server-ID: Rechtsklick auf deinen Server, „Server-ID kopieren“ → `DISCORD_GUILD_ID`
- Kanal-ID: Rechtsklick auf den Kanal, in den du Galix folgst, „Kanal-ID kopieren“ → `DISCORD_CHANNEL_IDS`. Mehrere IDs mit Komma trennen.
- Autor-ID: Gefolgte Nachrichten kommen über einen Webhook. Nach dem ersten übertragenen Galix-Beitrag lassen sich die IDs anzeigen:

  ```bash
  docker compose run --rm scrapedduck-ical-discord python /app/discord_worker.py --show-authors
  ```

  Die Zeile mit „Webhook/gefolgter Kanal“ ist der Galix-Follow. Diese ID kommt in `DISCORD_ALLOWED_AUTHOR_IDS`. Damit werden nur Galix' Beiträge verarbeitet, selbst wenn jemand anderes in deinem Kanal postet. Bleibt der Wert leer, wird jede Nachricht im Kanal verarbeitet.

IDs bitte immer aus Discord kopieren und nie raten. Der Worker prüft beim Start, ob der Kanal wirklich zur angegebenen Server-ID gehört.

### 5. Umgebungsvariablen eintragen

In der `.env`:

```bash
DISCORD_BOT_TOKEN=...
DISCORD_GUILD_ID=...
DISCORD_CHANNEL_IDS=...
DISCORD_ALLOWED_AUTHOR_IDS=...
```

Der Token steht nie in der `config.json`, in HTML, im Git-Repository oder im Log. Der Worker filtert ihn zusätzlich aus allen Logzeilen heraus.

### 6. Worker starten und neu starten

```bash
docker compose up -d scrapedduck-ical-discord
docker compose logs -f scrapedduck-ical-discord
docker compose restart scrapedduck-ical-discord   # nach Änderungen an der config.json
docker compose up -d                              # nach Änderungen an der .env (Container wird neu erstellt)
```

Einen einzelnen Abrufzyklus zum Testen startet `docker compose run --rm scrapedduck-ical-discord python /app/discord_worker.py --once`.

### Historischer Import

Beim allerersten Start eines Kanals werden die letzten `historical_import.message_limit` Nachrichten importiert (Standard 100). Mit `"enabled": false` wird nichts Altes importiert, dann geht es ab der neuesten Nachricht weiter. Danach holt der Worker nur noch neue Nachrichten ab der zuletzt gesehenen ID. Nach einem Neustart setzt er genau dort fort.

Den Import wiederholen, zum Beispiel mit einem anderen Limit:

```bash
docker compose stop scrapedduck-ical-discord
docker run --rm -v <ordner>_scrapedduck_ical_data:/data alpine rm /data/discord-worker-state.json
docker compose start scrapedduck-ical-discord
```

Bereits gespeicherte Grafiken werden dabei nicht doppelt angelegt.

### Was der Worker tut

- Er verarbeitet Bildanhänge und Bild-Embeds, auch mehrere pro Nachricht. Jede Grafik bekommt einen eigenen Datensatz mit der ID `<Nachrichten-ID>-<Anhang-ID>`.
- Er lädt nur per HTTPS von `cdn.discordapp.com` und `media.discordapp.net`. Weiterleitungen auf andere Hosts werden blockiert.
- Er prüft Endung, gemeldeten MIME-Typ, Größe (`max_file_size_mb`) und das echte Dateiformat anhand der ersten Bytes. Erlaubt sind JPEG, PNG und WebP.
- Duplikate erkennt er über die ID und über die SHA-256-Prüfsumme. Postet Galix dasselbe Bild erneut, wird es nicht noch einmal gespeichert.
- Bearbeitete Nachrichten erkennt er über die zuletzt geprüften Nachrichten (`recheck_recent_messages`). Der Text wird aktualisiert und die Grafik neu bewertet.
- Download-Fehler wiederholt er bis zu dreimal. Rate-Limits (HTTP 429) beachtet er, bei Netzwerkfehlern wartet er zunehmend länger und versucht es dann erneut.
- Das Original wird unverändert gespeichert:

  ```
  /data/
  ├── manual-events.json
  ├── infographic-events.json        ← Metadaten aller Grafiken
  ├── discord-worker-state.json      ← Status und letzte Nachrichten-ID je Kanal
  ├── pokemon-images/
  └── infographics/
      └── 2026/
          └── 11/
              └── 1438…-1438….png
  ```

Die Metadaten enthalten unter anderem Discord-IDs, den Link zur Originalnachricht in Galix' Server, Dateiname, Typ, Prüfsumme, Bildmaße, den OCR-Text, Matching-Status, Punkte, Begründung und Quellenangabe.

## Matching: wie Grafiken Events zugeordnet werden

Ausgewertet werden der Nachrichtentext und die OCR. Tesseract läuft lokal im Image mit Deutsch und Englisch, ohne Cloud-Dienst. Dabei gibt es zwei Durchgänge: das Originalbild und eine vergrößerte, invertierte Schwarzweiß-Version. So wird helle Schrift auf dunklem Grund genauso erkannt wie dunkle Schrift in hellen Kästen. Aus dem Text werden gelesen:

- Daten und Zeiträume: `4. NOV - 8. NOV`, `5 NOV`, `November 8–9`, `8.–9. November`, `24.-30.11.`, `08.11.2026`. Das Jahr ergibt sich aus dem Zeitpunkt des Posts.
- Uhrzeiten: `18:00 - 19:00`, `6 p.m.`
- Pokémon über den vorhandenen Namensindex mit deutschen und englischen Namen, Mega-, Dynamax- und Crypto-Formen inklusive
- der Eventtyp über Schlüsselwörter, zum Beispiel „Rampenlichtstunde/Spotlight“, „Raid-Tag/Raid Day“, „Raid-Stunde“, „Dyna-Montag“, „Community Day“, „Schwäche/Trainer benötigt“ für Raids
- Titelwörter, mit einem kleinen Wörterbuch Englisch→Deutsch (`matching.title_glossary`), etwa twilight → zwielicht

Für jedes Event im Kalender werden Punkte vergeben:

| Kriterium | Punkte |
| --- | --- |
| Datumsbereich exakt bzw. Datum eines Eintagesevents | 50 |
| Start- und Enddatum einzeln gefunden | 45 |
| Start- oder Enddatum | 35 |
| Datum liegt im Eventzeitraum | 15 |
| Uhrzeiten passen | 6–10 |
| Pokémon passt / widerspricht | 30 / −20 |
| Eventtyp passt / verwandt / widerspricht | 20 / 8 / −10 |
| Titelwörter | bis 20 |
| Post bis 30 Tage vor Eventende / nach Eventende | 10 / −40 |

Daraus folgt die Entscheidung:
- ab `auto_match_threshold` (Standard 80): automatische Zuordnung. Voraussetzung ist ein erkanntes Datum und ein Vorsprung von mindestens `min_margin` Punkten (Standard 15) vor dem zweitbesten Event.
- ab `manual_review_threshold` (Standard 50): Status „zu prüfen“, mit Vorschlägen im Adminbereich
- darunter: „offen“. Die Grafik wird bei jeder Kalenderaktualisierung neu bewertet, etwa sobald LeekDuck das Event führt.

Beispiele mit echten Galix-Grafiken:
- Giratina, 4.–8. Nov. Datum, Pokémon und Raid-Typ passen, das ergibt 110 Punkte und eine automatische Zuordnung. Shadow Tornadus im selben Zeitraum verliert wegen des anderen Pokémon.
- Teddiursa-Rampenlichtstunde, 5. Nov. Der Name ist im Bild verdeckt. Datum, 18:00–19:00 und „Rampenlichtstunde“ ergeben trotzdem 90 Punkte, also automatisch.
- Zwielicht-Wünsche, 24.–30. Nov. OCR erkennt nur „30 NOV“ und „10:00“, das sind 71 Punkte. Die Grafik kommt zur Prüfung, mit „Twilight Wishes“ als erstem Vorschlag.

Automatische Zuordnungen bleiben stabil. Manuelle Entscheidungen werden nie automatisch überschrieben.

## Wochenübersicht, Team GO Rocket und Neuigkeiten

Nicht jede Grafik gehört zu einem einzelnen Event. Jede Grafik bekommt deshalb eine Art, die automatisch erkannt wird und im Admin änderbar ist.

| Art | Erkennung | Anzeige | Gültig |
| --- | --- | --- | --- |
| Wochenübersicht („GO Weekly“) | mindestens fünf Wochentage (Montag … Sonntag) oder Stichwort „Weekly/Wochenübersicht“; Zeitraum wie „28. September – 4. Oktober“ | Leiste „Infografiken von G47IX“ über dem Kalender, mobil zusätzlich beim Antippen eines Tages dieser Woche in der Monatsansicht | im erkannten Wochenzeitraum, ab `weekly_lead_days` (3) Tage vorher als „Nächste Woche“ |
| Team GO Rocket | Übernahme/Takeover, Rocket, Giovanni, Sierra, Cliff, Arlo, Rüpel, Anführer; Start wie „startet am 2. Okt“ | Leiste und eigene Kachel „Aktuelles Rocket-Lineup“ bei den hilfreichen Links | immer die neueste Grafik, höchstens `rocket_max_age_days` (60) Tage |
| Neuigkeit | GO-Pass, Update, Spielqualität, Saison, Ankündigung – aber nur, wenn kein Event passt | Leiste | `news_days` (14) Tage ab Post, höchstens `news_max_items` (6) |
| Event | alles andere | Detailansicht des zugeordneten Events | wie bisher |

Die Wochenübersicht hat Vorrang vor Rocket, Rocket vor dem Event-Matching. Event-Grafiken, die nebenbei den GO-Pass erwähnen (zum Beispiel „GO-Pass Deluxe 5,49 €“), bleiben Event-Grafiken. Ein Klick auf eine Kachel öffnet die Grafik groß mit Quellenangabe. Die Werte stehen in der `config.json` unter `infographics.featured`.

## Manuelle Zuordnung im Adminbereich

Unter `/admin` steht der Bereich „Infografiken (G47IX)“. Er verwendet dasselbe Login und dieselbe CSRF-Absicherung wie die manuellen Events.

Zu jeder Grafik werden angezeigt:
- Vorschau
- Status: Offen, Zugeordnet, Zu prüfen, Abgelehnt oder Fehler
- Punkte und Begründung
- erkannte Daten und Pokémon
- Zeitpunkt des Posts und Link zu Discord
- Nachrichtentext und OCR

Möglich sind diese Aktionen:
- Art ändern: Event, Wochenübersicht, Team GO Rocket oder Neuigkeit (wird als manuelle Entscheidung gespeichert)
- Vorschlag zuordnen: ein Klick auf „Zuordnen: <Event> (<Punkte>)“
- andere Zuordnung wählen: Auswahl aus allen Events im Kalender, auch manuellen
- Zuordnung entfernen, Ablehnen
- automatisch neu bewerten: hebt eine manuelle Entscheidung auf
- Löschen: Datei und Datensatz werden entfernt, und die Grafik wird nicht erneut importiert
- Aufräumen: alle abgelehnten löschen oder alle, die älter als 120 Tage sind

Auf der Website erscheinen Grafiken nur beim zugeordneten Event, in der Detailansicht unter der Beschreibung. Bei mehreren Grafiken gibt es eine wischbare Galerie, ein Antippen öffnet die Grafik vergrößert. Darunter stehen die Quellenangabe und die Links zur Discord-Nachricht und zu G47IX. Monats-, Wochen- und Listenansicht bleiben unverändert.

## Konfiguration

Ausschnitt aus der `config.json` (alle Werte haben Standardwerte):

```json
"infographics": {
  "enabled": true,
  "storage_dir": "/data/infographics",
  "metadata_file": "/data/infographic-events.json",
  "worker_state_file": "/data/discord-worker-state.json",
  "max_file_size_mb": 15,
  "allowed_extensions": [".jpg", ".jpeg", ".png", ".webp"],
  "poll_interval_seconds": 300,
  "recheck_recent_messages": 25,
  "historical_import": { "enabled": true, "message_limit": 100 },
  "ocr": { "enabled": true, "languages": "deu+eng", "timeout_seconds": 90 },
  "matching": { "auto_match_threshold": 80, "manual_review_threshold": 50, "min_margin": 15 },
  "attribution": {
    "creator": "G47IX",
    "text": "Infografik von G47IX – mit freundlicher Genehmigung",
    "source_url": "https://linktr.ee/g47ix"
  }
}
```

- `"enabled": false` schaltet die gesamte Funktion ab. Dann werden keine Grafiken angezeigt und der Worker bleibt im Leerlauf.
- `"ocr": {"enabled": false}` schaltet nur die Texterkennung ab. Dann wird nur der Nachrichtentext ausgewertet.
- Weitere Optionen: `allowed_download_hosts`, `include_embed_images`, `download_timeout_seconds`, `matching.max_candidates`, `matching.title_glossary`.

### Eventarten und Filter

Welche ScrapedDuck-Eventarten im Kalender landen, steht unter `filters.include_types`. Die GO Naturzone (GO Wild Area) heißt dort `wild-area`. Weitere Arten, die standardmäßig fehlen, sind `timed-research`, `research`, `go-pass` und `season`.

`filters.global_only_types` (Standard: `wild-area`, `pokemon-go-tour`, `pokemon-go-fest`) zeigt bei diesen Arten nur die globale Ausgabe. Lokale Vor-Ort-Ausgaben wie „Wild Area: Sendai“ oder „Tour: Los Angeles“ werden ausgeblendet.

Änderungen an der `config.json` greifen im Webserver sofort. Den Worker danach neu starten.

## HAProxy und Cloudflare

Leitet HAProxy den ganzen Host an Port 8080 weiter, ist nichts zu tun. Bei einer Pfad-Whitelist kommen zu den bisherigen Pfaden

```text
/admin
/api/admin/session
/api/admin/login
/api/admin/logout
/api/admin/events
/api/admin/events/*
```

neu hinzu:

```text
/infographic/*
/api/admin/infographics
/api/admin/infographics/*
```

Der Worker braucht keinen Port und keine Freigabe. Er stellt nur ausgehende HTTPS-Verbindungen zu `discord.com` und `cdn.discordapp.com` her. Infografiken werden mit `Cache-Control: public, max-age=86400` ausgeliefert, Adminantworten mit `no-store`.

## Backup und Wiederherstellung

Alle Daten liegen im Volume `<ordner>_scrapedduck_ical_data`. Komplett sichern:

```bash
docker run --rm -v <ordner>_scrapedduck_ical_data:/data:ro -v "$PWD":/backup alpine \
  tar czf /backup/pogo-kalender-$(date +%F).tar.gz -C /data .
cp .env config.json /pfad/zum/backup/
```

Wiederherstellen:

```bash
docker compose down
docker run --rm -v <ordner>_scrapedduck_ical_data:/data -v "$PWD":/backup alpine \
  sh -c "cd /data && tar xzf /backup/pogo-kalender-JJJJ-MM-TT.tar.gz"
docker compose up -d
```

Nur die manuellen Events sichern:

```bash
docker cp scrapedduck-ical:/data/manual-events.json ./manual-events-backup.json
```

## Update und Rollback

Update auf den neuesten Stand:

```bash
docker compose pull
docker compose up -d
```

Jedes Image trägt zusätzlich den Commit als Tag, zum Beispiel `ghcr.io/emrewest/pogo-kalender:sha-5f6a9cf`. Für ein Rollback in der `docker-compose.yml` bei beiden Diensten `:latest` durch den gewünschten Tag ersetzen und `docker compose up -d` ausführen. Alle Versionen stehen unter „Packages“ im GitHub-Repository.

Zurück auf den alten Stand ohne Infografiken (1.6.0, Tag `sha-5f6a9cf`):
1. Den Worker-Dienst aus der `docker-compose.yml` entfernen und das Image auf `sha-5f6a9cf` setzen.
2. Achtung: 1.6.0 kennt `ADMIN_PASSWORD` nicht. Das Passwort muss dann wieder in der `config.json` stehen.
3. Die neuen Dateien im Volume stören die alte Version nicht.

Vor jedem Update ein Backup machen.

## Fehlerdiagnose

| Symptom | Ursache / Lösung |
| --- | --- |
| Worker-Log: „Fehlende Discord-Konfiguration“ | `.env` fehlt, liegt nicht im Compose-Ordner oder ein Wert ist leer. Danach `docker compose up -d` ausführen, nicht nur restart. |
| `Discord-API 401` | Token falsch oder zurückgesetzt, neuen Token in die `.env` eintragen |
| `Discord-API 403 Missing Access` | Bot fehlt im Kanal, oder „View Channel“ bzw. „Read Message History“ fehlt |
| `Discord-API 404` | Kanal-ID falsch oder Bot nicht im Server |
| „gehört nicht zum konfigurierten Server“ | `DISCORD_GUILD_ID` passt nicht zum Kanal |
| Nachrichten kommen an, aber keine Bilder | Message Content Intent im Developer Portal nicht aktiv, oder `DISCORD_ALLOWED_AUTHOR_IDS` enthält nicht die Webhook-ID (`--show-authors`) |
| Grafik bleibt „Offen“ | Das Event steht noch nicht bei ScrapedDuck, oder es liegt außerhalb von `past_days`/`future_days`. Sonst im Admin manuell zuordnen. |
| Grafik „Fehler“ | Download fehlgeschlagen, wird bis zu dreimal wiederholt. Der Grund steht in der Vorschau. |
| OCR-Status „error“ | Tesseract fehlt (nur bei eigenem Build ohne die apk-Pakete) oder Timeout. Das Matching nutzt dann nur den Nachrichtentext. |

Hilfreiche Befehle:

```bash
docker compose logs --tail 200 scrapedduck-ical-discord
docker compose exec scrapedduck-ical-discord cat /data/discord-worker-state.json
curl -s http://SERVER-IP:8080/health | python3 -m json.tool
docker compose ps        # Healthcheck-Status beider Container
```

Die Statusseite `/status` zeigt die Zahl der Infografiken und den Zustand des Workers.

## Entwicklung und Tests

Die Tests kommen ohne echte Discord-Zugangsdaten aus. Discord wird mit Testnachrichten simuliert, der OCR-Test läuft nur, wenn Tesseract installiert ist.

```bash
pip install -r requirements.txt
python -m unittest discover -s tests -t . -v
```

GitHub Actions führt bei jedem Push die Tests aus. Danach baut es das Image, prüft Webserver, iCal-Feed und Worker-Leerlauf im Container, führt die Tests im fertigen Image samt Tesseract erneut aus und veröffentlicht es nach `ghcr.io/emrewest/pogo-kalender`.

Daten: LeekDuck.com via ScrapedDuck. Infografiken: G47IX, mit freundlicher Genehmigung.
