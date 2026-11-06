# Konfiguration und Funktionen

Dieses Dokument beschreibt alle Umgebungsvariablen und alle Funktionen des Stream-Recorders.
Wie die Container technisch zusammenarbeiten, steht in [ARCHITECTURE.md](ARCHITECTURE.md).
Beispiele für den Aufbau mit Jellyfin und NAS stehen in [DEPLOYMENT.md](DEPLOYMENT.md).

- [Überblick: drei Container](#überblick-drei-container)
- [Wo welche Einstellung herkommt](#wo-welche-einstellung-herkommt)
- [Umgebungsvariablen](#umgebungsvariablen)
- [Funktionen](#funktionen)

## Überblick: drei Container

Alle drei Dienste laufen aus demselben Quellcode. `recorder` und `web` nutzen das Image-Target
`runner`, `converter` das Target `converter` (zusätzlich mit ffmpeg).

| Dienst | Start | Aufgabe |
|---|---|---|
| `recorder` | `python -m app.recorder` | Prüft Kanäle bei Twitch, nimmt Live-Streams auf, lädt VODs, räumt `done/` auf |
| `converter` | `python -m app.converter` | Wandelt Aufnahmen in MP4 mit Kapiteln um und legt sie in der Mediathek ab. Beliebig viele, auch auf anderen Hosts |
| `web` | `python -m app.web` | Weboberfläche und JSON-API auf Port 8080 |

Healthcheck für den Recorder: `python -m app.healthcheck` (in `docker-compose.yml` eingetragen,
bei `docker run` per `--health-cmd`). Ohne Compose startet man jeden Container einzeln, siehe
[DEPLOYMENT.md](DEPLOYMENT.md#einzelne-container-ohne-compose).

## Wo welche Einstellung herkommt

Es gibt zwei Arten von Einstellungen:

1. **Nur per Umgebungsvariable:** Pfade, Zugangsdaten, Zugriffsschutz, Name des Konverters.
   Diese können absichtlich nicht über die Weboberfläche geändert werden. So kann man sich
   nicht aussperren, und Zugangsdaten landen nicht in einer Datei, die das Webinterface schreibt.
2. **Im Webinterface änderbar (Bereich „Einstellungen“):** Die Umgebungsvariable liefert nur den
   **Standardwert**. Was im Webinterface gespeichert wird, steht in `/config/streams.json` unter
   `settings` und hat Vorrang. „Auf Standard zurücksetzen“ entfernt die gespeicherten Werte wieder.
   Der Recorder übernimmt Änderungen innerhalb einer Sekunde, ohne Neustart.

Kanäle (URL, Ausgabeordner, Qualität, aktiv) werden nur im Webinterface gepflegt
(`/config/streams.json` unter `streams`).

Umgebungsvariablen werden in `default.env` (allgemein) und `clientid.env` (Zugangsdaten,
nicht im Git) gesetzt. Die Compose-Variablen (`PUID`, `WORK_PATH`, …) setzt man in einer `.env`
neben der `docker-compose.yml` oder in der Shell.

## Umgebungsvariablen

Spalte „Dienst“: **R** = recorder, **C** = converter, **W** = web.

### Twitch-Zugang

| Variable | Alias (alt) | Standard | Dienst | Bedeutung |
|---|---|---|---|---|
| `TWITCH_CLIENT_ID` | `clientid` | – (Pflicht) | R | Client-ID einer Twitch-App (<https://dev.twitch.tv/console/apps>). Ohne startet der Recorder nicht |
| `TWITCH_CLIENT_SECRET` | `clientsecret` | – (Pflicht) | R | Secret dieser App |
| `TWITCH_OAUTH_TOKEN` | `TWITCH_AUTH_TOKEN`, `oauthtoken` | – | R | Optional: Browser-Cookie `auth-token` eines Twitch-Kontos. Damit gelten Turbo/Abos (werbefrei), und abonnentenexklusive VODs lassen sich laden. Wie ein Passwort behandeln |

Die Client-ID und das Secret werden nur für die offizielle Helix-API benutzt: Live-Status und
Kategorie, VOD-Metadaten. Der OAuth-Token geht nur an Twitchs API (streamlink-Option
`api-header`), nie an die Video-CDN.

### Zugriff auf das Webinterface

| Variable | Standard | Dienst | Bedeutung |
|---|---|---|---|
| `ALLOWED_NETWORKS` | alle privaten Netze: `127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,fc00::/7` | W | Kommagetrennte IPs oder Netze, die das Webinterface nutzen dürfen. Alle anderen bekommen `403`. Die mitgelieferte `default.env` setzt `10.0.0.0/24,127.0.0.1` |
| `WEB_PORT` | `8080` | W (+ Compose) | Port im Container. In der Compose-Datei zusätzlich der Port auf dem Host |
| `WEB_TLS` | `true` | W | Webinterface per HTTPS. `false` nur hinter einem Reverse Proxy, der selbst TLS macht |
| `TLS_HOSTNAMES` | leer | W | Zusätzliche Namen/IPs für das selbstsignierte Zertifikat, z. B. `10.0.0.196,nas.local`. `localhost`, `127.0.0.1`, `::1` und der Container-Hostname sind immer enthalten. Fehlt ein Name im Zertifikat, wird es neu erzeugt |
| `TLS_CERT_FILE`, `TLS_KEY_FILE` | `/config/tls/cert.pem`, `/config/tls/key.pem` | W | Eigenes Zertifikat (z. B. von einer eigenen CA). Beide oder keine setzen. Ein eigenes Zertifikat wird nie ersetzt |

**HTTPS:** Beim ersten Start erzeugt der Web-Container ein selbstsigniertes Zertifikat (ECDSA
P-256, 10 Jahre gültig) in `/config/tls/`. Der Schlüssel bekommt die Rechte `0600`. Weniger als
30 Tage vor Ablauf wird es erneuert. Der Ordner wird über das Web nie ausgeliefert. Das Log zeigt
beim Start den SHA-256-Fingerabdruck. Den kann man mit der Zertifikatswarnung des Browsers
vergleichen, bevor man die Ausnahme bestätigt. Erlaubt sind TLS 1.2 und neuer, auf HTTP antwortet
der Port nicht. Wird das Zertifikat neu erzeugt (neuer Name in `TLS_HOSTNAMES`, Ablauf), muss
der Browser es erneut bestätigen.

Zugriffe auf `localhost` des Docker-Hosts kommen im Container von der Bridge-Adresse an (z. B.
`172.19.0.1`). Für einen Reverse Proxy muss dessen IP erlaubt sein.

### Pfade im Container

Normalerweise nicht ändern, die Compose-Dateien hängen die Volumes genau dort ein.

| Variable | Standard | Dienst | Bedeutung |
|---|---|---|---|
| `CONFIG_DIR` | `/config` | R, W | `streams.json`, `status.json`, `downloads/`, `commands/` |
| `WORK_DIR` | `/work` | R, C, W | Arbeitsordner und Warteschlange (siehe [ARCHITECTURE.md](ARCHITECTURE.md#der-arbeitsordner-work)) |
| `RECORDINGS_DIR` | `/recordings` | C | Ziel der fertigen MP4s, also die Mediathek |
| `TZ` | `Europe/Berlin` (in `default.env`) | alle | Zeitzone für Datei- und Ordnernamen und Logs |

### Konverter

| Variable | Standard | Dienst | Bedeutung |
|---|---|---|---|
| `CONVERTER_NAME` | Hostname des Containers | C | Name in der Weboberfläche („Worker“). Bei mehreren Konvertern eindeutig wählen |
| `CONVERTER_POLL_INTERVAL` | `10` | C | Sekunden Wartezeit, wenn die Warteschlange leer ist. Bestimmt auch, ab wann ein Konverter als offline gilt: kein Lebenszeichen seit max(60 s, 3 × Intervall) |

### Im Webinterface änderbare Einstellungen (Standardwerte)

| Variable (Alias) | Standard | Bereich | Webinterface | Bedeutung |
|---|---|---|---|---|
| `CHECK_INTERVAL` (`timer`) | `60` | 10–3600 s | Prüfintervall | Wie oft alle aktiven Kanäle bei Twitch geprüft werden (ein API-Aufruf je 100 Kanäle) |
| `DEFAULT_QUALITY` (`quality`) | `best` | Liste | Standardqualität | Qualität für Kanäle und VODs ohne eigene Angabe, z. B. `1080p60,720p60,best`. Die erste vorhandene gewinnt, sonst `best` |
| `RESCAN_INTERVAL` | `10` | 5–600 s | Nachprüfung alle | Nach dem Ende eines Streams wird der Kanal so oft direkt über streamlink geprüft |
| `RESCAN_DURATION` | `120` | 0–3600 s | Nachprüfung für | Wie lange nach Stream-Ende schnell geprüft wird. `0` schaltet die Nachprüfung ab |
| `MAX_PARALLEL_STREAMS` | `0` | 0–100 | Max. parallele Twitch-Streams | Live-Aufnahmen plus VOD-Downloads. Ist das Limit erreicht, startet kein neuer VOD-Download. Live-Aufnahmen laufen immer. `0` = kein Limit |
| `SLOT_COOLDOWN` | `120` | 0–3600 s | Platz reservieren nach Stream-Ende | So lange hält ein Kanal nach Stream-Ende seinen Platz im Limit. `0` = sofort frei |
| `FOLDER_LAYOUT` | `output_year_month` | siehe unten | Unterordner | Ordnerstruktur in der Mediathek |
| `KEEP_SOURCE` | `true` | true/false | Original behalten | Die aufgenommene `.ts` nach der Umwandlung in `/work/done/` aufheben statt löschen |
| `DONE_RETENTION_DAYS` | `0` | 0–3650 Tage | Originale löschen nach | Aufgehobene Originale nach so vielen Tagen automatisch löschen (stündlich, durch den Recorder). `0` = nie |
| `AUDIO_NORMALIZE` | `true` | true/false | Lautstärke normalisieren | `loudnorm=I=-14:TP=-1.5:LRA=11`, Audio als AAC 192k. Aus: Audio wird unverändert kopiert |
| `VIDEO_CODEC` | `copy` | Codec-Name | Video-Codec | `copy` übernimmt das Video unverändert. Jeder ffmpeg-Encoder (`libx264`, `libx265`, `libsvtav1`, …) kodiert neu |
| `FFMPEG_ARGS` | leer | max. 500 Zeichen | Eigene ffmpeg-Parameter | Zusätzliche ffmpeg-Ausgabeoptionen, z. B. `-crf 23 -preset medium`. Siehe [Umwandlung](#umwandlung-in-mp4) |
| `VOD_MODE` | `realtime` | `realtime`, `download` | VOD-Modus | Standardmodus für VOD-Downloads |
| `VOD_SPEED` | `1` | 0,25–20 | VOD-Tempo (Echtzeit) | Tempo im Echtzeit-Modus, 1 = Wiedergabegeschwindigkeit |
| `VOD_SEGMENT_THREADS` | `3` | 1–10 | VOD-Download-Threads | Parallele Segment-Downloads im Download-Modus |
| `VOD_PARALLEL` | `1` | 1–5 | Gleichzeitige VOD-Downloads | Höchstzahl gleichzeitiger VOD-Downloads (zusätzlich zu `MAX_PARALLEL_STREAMS`) |

Werte für `FOLDER_LAYOUT` (Beispiel: Ausgabeordner `Twitch/Name`, Start 6.11.2026 20:15):

| Wert | Ergebnis |
|---|---|
| `output_year_month` | `Twitch/Name-2026/11/2026-11-06 20-15-00 - Titel.mp4` (wie der frühere ffmpeg-Container) |
| `year_month` | `Twitch/Name/2026/11/2026-11-06 20-15-00 - Titel.mp4` |
| `year` | `Twitch/Name/2026/2026-11-06 20-15-00 - Titel.mp4` |
| `date` | `Twitch/Name/2026-11-06/2026-11-06 20-15-00 - Titel.mp4` |
| `none` | `Twitch/Name/2026-11-06 20-15-00 - Titel.mp4` |

### Compose-Variablen

Diese werden nur von `docker-compose.yml` und `docker-compose.converter.yml` ausgewertet.

| Variable | Standard | Bedeutung |
|---|---|---|
| `PUID`, `PGID` | `1027`, `100` | Benutzer und Gruppe, unter denen alle Container laufen. Müssen auf `config`, `work` und `recordings` schreiben dürfen (`1027:100` ist der erste Admin bzw. die Gruppe `users` auf Synology) |
| `CONFIG_PATH` | `./config` | Host-Ordner für `/config` |
| `WORK_PATH` | `./work` | Host-Ordner für `/work` (bei Konvertern auf anderen Hosts eine gemeinsame Freigabe) |
| `RECORDINGS_PATH` | `./recordings` | Host-Ordner für `/recordings`, also die Mediathek |
| `WEB_PORT` | `8080` | Port des Webinterface auf dem Host |
| `CONVERTER_NAME` | leer (Hostname) | siehe oben |

## Funktionen

### Kanäle

- Kanal eintragen als Login-Name oder URL (`https://www.twitch.tv/name`), mit optionalem
  **Ausgabeordner** und optionaler **Qualität**.
- Der Ausgabeordner liegt relativ zu `/recordings` und darf Unterordner enthalten (`Twitch/Name`).
  Leer bedeutet: der Kanalname. `..` und unsichere Zeichen werden abgelehnt.
- **Aktiv** abwählen stoppt eine laufende Aufnahme. Entfernen ebenso.
- Statusspalte: nimmt auf / live / offline / Nachprüfung / Fehler, dazu aktuelle Kategorie und
  Titel sowie die Zieldatei der laufenden oder letzten Aufnahme.

### Live-Aufnahme

- Alle `CHECK_INTERVAL` Sekunden fragt der Recorder für alle aktiven Kanäle bei Helix nach, wer
  live ist.
- Für jeden Kanal, der live ist, läuft eine Aufnahme in einem eigenen Thread. Sie schreibt
  per streamlink nach `/work/recording/<job>/video.ts.part`.
- Bei jedem Kategorie-Wechsel kommt eine Zeile in `chapters.csv` (Versatz ab Aufnahmestart,
  Zeitstempel, Kategorie-ID und -Name, Titel). Der Wechsel wird erst beim nächsten Check
  erkannt, Kapitel beginnen also bis zu einem Prüfintervall zu spät.
- Am Stream-Ende wird aus `.part` die `.ts`, und der Job geht in die Warteschlange
  (`/work/queue/`).
- Bei einem Fehler wird trotzdem umgewandelt. Der Job ist dann als „unvollständig“ markiert.
- Bei `docker compose down` werden laufende Aufnahmen sauber geschlossen und eingereiht. Nach
  einem Absturz reiht der Recorder beim Start liegengebliebene Aufnahmen ein.

### Schnelle Nachprüfung nach Stream-Ende

Endet eine Aufnahme, prüft der Recorder den Kanal `RESCAN_DURATION` Sekunden lang alle
`RESCAN_INTERVAL` Sekunden. Er fragt dabei **direkt über streamlink** ab, nicht über Helix.
Helix meldet einen neu gestarteten Stream oft erst 30–90 s später. Ist der Stream zurück, beginnt
eine neue Datei mit der zuletzt bekannten Kategorie. Ein unterbrochener Stream ergibt also zwei
Dateien.

### Limit für parallele Twitch-Streams

- `MAX_PARALLEL_STREAMS` zählt laufende Live-Aufnahmen, VOD-Downloads und Kanäle, deren Platz
  nach Stream-Ende noch reserviert ist.
- Ein wartender VOD-Download startet nur, wenn noch ein Platz frei ist. Er zeigt dann
  „wartet (Limit erreicht)“.
- **Live-Aufnahmen werden nie blockiert:** Ein verpasster Live-Stream ist weg, ein VOD kann warten.
  Laufende Downloads werden nicht abgebrochen, wenn danach Live-Streams dazukommen.
- **Reservierung nach Stream-Ende:** Ein Kanal hält seinen Platz `SLOT_COOLDOWN` Sekunden lang,
  damit er bei einer Unterbrechung sicher wieder aufnehmen kann. Die Kanalliste zeigt „Stream-Platz
  reserviert bis …“ mit dem Button **„Platz freigeben“**. Die Nachprüfung läuft nach dem
  Freigeben weiter.
- Über der VOD-Liste steht „Twitch-Streams: X von Y belegt (davon Z reserviert nach Stream-Ende)“.

### VOD-Downloads

- Bereich „VOD-Downloads“: VOD-URL (`https://www.twitch.tv/videos/<id>`), optional
  Ausgabeordner, Qualität, Modus und Tempo.
- **Echtzeit (Standard):** streamlink löst das VOD auf (Zugriffstoken, Playlist). Die Segmente
  holt der Recorder dann selbst nacheinander im Wiedergabetempo × Tempo (mit 10 s
  Wiedergabezeit Vorlauf). So verteilt sich die Bandbreite über die Länge des VODs. Ein 3-h-VOD
  dauert bei Tempo 1 drei Stunden, bei Tempo 4 etwa 45 Minuten.
- **Download:** das normale Verhalten von streamlink, so schnell wie möglich mit
  `VOD_SEGMENT_THREADS` parallelen Segmenten.
- Warum nicht streamlink selbst drosselt: Beim Schreiben in eine Datei lädt streamlink so schnell
  das Netz kann. Auch bei gebremstem Lesen holt es bis zu 20 Segmente plus 16 MB Puffer im Voraus
  (gemessen: alle Segmente eines 2-min-Test-VODs nach 0,4 s).
- **Benennung wie bei Aufnahmen:** Titel, Kanal und Startzeit kommen aus Helix, mit den
  Metadaten von streamlink als Rückfall. Ohne eigenen Ausgabeordner gilt der Ordner des Kanals,
  falls er in der Kanalliste steht, sonst der Kanalname.
- **Kapitel:** ein Kapitel mit der Kategorie des VODs. Die offizielle API liefert keine
  Kategorie-Wechsel innerhalb eines VODs.
- Abonnentenexklusive VODs brauchen `TWITCH_OAUTH_TOKEN`.
- Abbrechen, Erneut versuchen und Entfernen gehen in der Liste. Ein Abbruch oder ein Neustart des
  Recorders verwirft halbe Downloads, und wartende Downloads beginnen danach von vorn.
- Die Liste zeigt Fortschritt, geladene Zeit von Gesamtlänge, Größe, tatsächliches Tempo und
  Restzeit.

### Umwandlung in MP4

- Ein Konverter holt den ältesten Job aus `/work/queue/` und ruft ffmpeg auf:
  - Video: `copy` oder `VIDEO_CODEC`.
  - Audio: normalisiert oder kopiert.
  - Kapitel aus `chapters.csv`.
  - Metadaten: `title` (Stream-Titel), `artist`/`album_artist` (Kanal), `date`, `comment`
    (Kanal und Kategorien).
  - `-movflags +faststart`.
- **Eigene ffmpeg-Parameter** stehen direkt vor der Ausgabedatei und überschreiben damit die
  Optionen davor (ffmpeg nimmt den letzten Wert), z. B. `-c:a copy` oder `-vf scale=-2:720`.
  Abgelehnt werden `-i`, `-progress`, `-y`, `-n`, `-stdin`, `-nostdin`. Die Parameter werden
  ohne Shell übergeben.
- Die Konvertierungs-Einstellungen werden beim **Start der Aufnahme** in den Job geschrieben,
  weil die Konverter `/config` nicht kennen. „Erneut versuchen“ bei einem fehlgeschlagenen Job
  übernimmt die **aktuellen** Einstellungen.
- **Ablage:** Gibt es die Datei schon, wird `_1`, `_2`, … angehängt. Über Dateisystemgrenzen wird
  erst unter einem versteckten Namen kopiert und dann umbenannt, damit Jellyfin nie eine halbe
  Datei sieht.
- **Danach:** Das Original geht nach `/work/done/` („Original behalten“) oder wird gelöscht.
  Schlägt die Umwandlung fehl, landet der Job in `/work/failed/`, mit der Fehlermeldung von ffmpeg.

### Übersicht „Verarbeitung“

- Kacheln: Jobs offen, Jobs laufen, Worker online, Jobs abgeschlossen, fehlgeschlagen,
  Aufnahmen laufen, Originale aufgehoben (Anzahl und Größe).
- Je laufender Umwandlung: Fortschritt, Konverter, Laufzeit, geschätzte Restzeit.
- Liste der Worker (arbeitet / bereit / offline seit …) und der zuletzt fertigen Dateien.
- Tabelle aller Jobs. Fehlgeschlagene können erneut versucht oder gelöscht werden.
- „Aufgehobene Originale“: Liste mit Größe, einzeln oder alle löschen. Die MP4s bleiben dabei
  unangetastet.

### Zugriffsschutz und Sicherheit

- Das Webinterface läuft per HTTPS (siehe oben), die Container untereinander kommunizieren nur
  über Dateien. Die HTTP-API ist in [`app/static/openapi.yaml`](../app/static/openapi.yaml)
  beschrieben (OpenAPI 3.1, auch abrufbar unter `/api/openapi.yaml`).
- Das Webinterface antwortet nur Adressen aus `ALLOWED_NETWORKS`. Ändernde API-Aufrufe verlangen
  `Content-Type: application/json`, damit fremde Webseiten keine Formulare absenden können.
- Alle Container laufen ohne Root-Rechte mit schreibgeschütztem Dateisystem, ohne Capabilities
  und mit `no-new-privileges`. Beschreibbar sind nur die Volumes und `/tmp`.
- Ausgabepfade aus Benutzereingaben werden bereinigt und bleiben immer unterhalb von
  `/recordings`.
- Fehlermeldungen von Twitch, streamlink und HTTP werden nie im Wortlaut geloggt oder angezeigt,
  nur der Fehlertyp. Sie können URLs mit dem Client-Secret enthalten.
- Mit eigenen ffmpeg-Parametern kann man ffmpeg zusätzliche Ausgabedateien schreiben lassen,
  innerhalb von `/work` und `/recordings`. Deshalb gehört das Webinterface nur ins Heimnetz.
