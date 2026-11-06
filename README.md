# ScrapedDuck iCal – Webkalender mit manuellen Events

Der Dienst lädt die ScrapedDuck-Events, filtert und übersetzt sie und stellt sie als Webkalender sowie abonnierbaren `.ics`-Feed bereit. Zusätzlich können eigene Events über eine passwortgeschützte Weboberfläche angelegt, bearbeitet und gelöscht werden.

## Installation oder Update

Die Projektdateien ersetzen und anschließend vollständig neu bauen:

```bash
docker compose down
docker compose build --no-cache
docker compose up -d
docker compose logs -f scrapedduck-ical
```

Das vorhandene Docker-Volume `scrapedduck_ical_data` bleibt erhalten. Pokémon-Bilder, Übersetzungs-Cache und manuelle Events gehen beim Neubau daher nicht verloren.

## Öffentliche Endpunkte

- `/` – Webkalender mit Monats-, Wochen- und Listenansicht
- `/pokemon-go-de.ics` – abonnierbarer iCalendar-Feed
- `/api/events` – Events und Metadaten für die Webansicht
- `/status` – visuelle Statusseite
- `/health` – maschinenlesbarer JSON-Status
- `/admin` – passwortgeschützte Verwaltung manueller Events

Der unauffällige Link **Login** befindet sich außerdem ganz unten auf der Kalenderseite.

## Admin-Passwort ändern

In der mitgelieferten `config.json` ist zunächst dieses generische Passwort gesetzt:

```text
CHANGE-ME-NOW-2026!
```

Vor der öffentlichen Nutzung unbedingt ersetzen:

```json
"manual_events": {
  "enabled": true,
  "file": "/data/manual-events.json",
  "admin_password": "HIER-EIN-LANGES-EIGENES-PASSWORT-EINTRAGEN",
  "session_hours": 12,
  "secure_cookie": true,
  "max_events": 500,
  "login_attempts": 5,
  "login_window_seconds": 900
}
```

Danach den Container neu starten:

```bash
docker compose restart scrapedduck-ical
```

Die Admin-Sitzung verwendet ein `HttpOnly`-, `SameSite=Strict`- und standardmäßig ein `Secure`-Cookie. Deshalb sollte die Verwaltung über die öffentliche HTTPS-Adresse geöffnet werden:

```text
https://pogo-kalender.emrebati.de/admin
```

Für einen ausschließlich lokalen HTTP-Test kann `secure_cookie` vorübergehend auf `false` gesetzt werden. Öffentlich sollte es immer `true` bleiben.

## Manuelle Events verwalten

Nach dem Login können folgende Angaben gepflegt werden:

- Titel und Eventtyp
- ganztägig oder Start-/Enduhrzeit
- Beschreibung
- Ort
- weiterführender Link
- Pokémon-Namen für passende Bilder

Die Oberfläche unterstützt Anlegen, Bearbeiten und Löschen. Änderungen werden sofort für den nächsten Abruf von Webansicht und iCal-Feed vorgemerkt.

Vordefinierte Eventtypen und Farben:

- `manual-event` – allgemeines manuelles Event
- `manual-meetup` – Treffen
- `manual-raid` – Raid-Treffen
- `manual-reminder` – Erinnerung

Es können auch eigene Typen aus Kleinbuchstaben, Zahlen und Bindestrichen verwendet werden.

## Speicherung der manuellen Events

Die Daten werden atomar in diese Datei im persistenten Docker-Volume geschrieben:

```text
/data/manual-events.json
```

Anzeigen:

```bash
docker exec scrapedduck-ical cat /data/manual-events.json
```

Sichern:

```bash
docker cp scrapedduck-ical:/data/manual-events.json ./manual-events-backup.json
```

Wiederherstellen:

```bash
docker cp ./manual-events-backup.json scrapedduck-ical:/data/manual-events.json
docker compose restart scrapedduck-ical
```

Die Weboberfläche zeigt bei ganztägigen Events ein einschließliches Enddatum. In der gespeicherten JSON-Datei ist das Ende technisch exklusiv, wie bei iCalendar üblich. Ein Event nur am 22. August wird daher so gespeichert:

```json
{
  "start": "2026-08-22",
  "end": "2026-08-23",
  "all_day": true
}
```

## Pokémon-Bilder bei manuellen Events

Im Feld **Pokémon für Bilder** können mehrere Namen kommasepariert eingetragen werden:

```text
Rayquaza, Kyogre, Groudon
```

Deutsche und englische Namen werden unterstützt. Die Bilder werden über denselben lokalen Cache bereitgestellt wie bei den automatisch importierten Raid- und Dyna-Events.

## HAProxy und Cloudflare

Wenn das HAProxy-Frontend den gesamten Host `pogo-kalender.emrebati.de` an das Backend weiterleitet, ist keine weitere Änderung nötig.

Bei einer Pfad-Whitelist müssen zusätzlich freigegeben werden:

```text
/admin
/api/admin/session
/api/admin/login
/api/admin/logout
/api/admin/events
/api/admin/events/*
```

Die Adminantworten tragen `Cache-Control: no-store`, damit Cloudflare und Browser keine vertraulichen Inhalte zwischenspeichern.

## Sicherheitsfunktionen

- kein Benutzername, nur Passwort
- konstanter Passwortvergleich
- zeitlich begrenzte In-Memory-Sitzungen
- `HttpOnly`-, `SameSite=Strict`- und Secure-Cookie
- CSRF-Token für schreibende Anfragen
- Begrenzung fehlgeschlagener Loginversuche
- atomare JSON-Schreibvorgänge
- Größen- und Formatprüfung aller Eingaben
- URLs sind nur mit `http://` oder `https://` erlaubt

Ein Container-Neustart beendet automatisch alle bestehenden Admin-Sitzungen.

## Feed testen

```bash
curl -I http://SERVER-IP:8080/pokemon-go-de.ics
curl -s http://SERVER-IP:8080/health
```

Öffentlich:

```text
https://pogo-kalender.emrebati.de/pokemon-go-de.ics
```


Hinweis: Im Pokémon-Feld manueller Events werden deutsche und englische Pokémon-Namen erkannt.

## Deutsche Pokémon-Namen bei manuellen Events

Das Feld „Pokémon für Bilder“ akzeptiert sowohl englische als auch deutsche Namen, zum Beispiel `Charizard` oder `Glurak`. Mehrere Namen werden durch Kommas getrennt. Zusätze wie `Mega Glurak X` oder `Dynamax Barschwa` werden ebenfalls erkannt; für das Bild wird die zugrunde liegende Pokémon-Art verwendet.
