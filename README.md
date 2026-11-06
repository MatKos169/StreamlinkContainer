# Streamlink Recorder

Nimmt Twitch-Streams automatisch auf und legt sie als MP4 mit Kapiteln in deiner Mediathek ab
(z. B. für Jellyfin). Die Kanäle trägst du in einer Weboberfläche ein. Ein Recorder-Container
überwacht sie und nimmt jeden Kanal auf, der live geht. Ein Konverter-Container macht daraus
MP4-Dateien mit einem Kapitel pro Twitch-Kategorie, normalisiertem Ton und Metadaten.
Twitch-VODs lassen sich ebenfalls herunterladen.

Fork von [liofal/streamlink](https://github.com/liofal/streamlink). Die Konvertierung orientiert sich
an [MatKos169/ffmpeg](https://github.com/MatKos169/ffmpeg).

## Funktionen

- **Mehrere Kanäle**, jeder mit eigenem Ausgabeordner und eigener Qualität. Verwaltet in der
  Weboberfläche, die Änderungen wirken ohne Neustart.
- **Live-Aufnahme** mit streamlink. Kategorie-Wechsel werden als Kapitel mitgeschrieben.
- **Schnelle Nachprüfung nach Stream-Ende:** Bricht ein Stream kurz ab, wird der Neustart in
  Sekunden erkannt, nicht erst beim nächsten regulären Check.
- **VOD-Downloads** im Echtzeit-Modus (Standard: lädt im Wiedergabetempo, spart Bandbreite und
  Last bei Twitch) oder im Download-Modus (so schnell wie möglich).
- **Limit für parallele Twitch-Streams:** VOD-Downloads warten, bis ein Platz frei ist. Nach einem
  Stream-Ende bleibt der Platz eine Weile reserviert. Live-Aufnahmen laufen immer.
- **Umwandlung in MP4** per ffmpeg:
  - Kapitel, Titel, Kanal, Datum.
  - Lautstärke-Normalisierung (loudnorm -14 LUFS).
  - Video wird kopiert oder mit wählbarem Codec neu kodiert, eigene ffmpeg-Parameter möglich.
- **Prüfung der Twitch-Zugangsdaten:**
  - App-Zugang und OAuth-Token werden regelmäßig und auf Knopfdruck geprüft, mit Hinweis vor dem
    Ablauf des Tokens.
  - Bei ungültigem Token wird wahlweise ohne Token weiter aufgenommen oder ausgesetzt.
- **Ordnerstruktur für Jellyfin**, z. B. `Name-2026/11/2026-11-06 20-15-00 - Titel.mp4`.
- **Mehrere Konverter**, auch auf anderen Hosts, über einen gemeinsamen Arbeitsordner.
- **Übersicht:** offene, laufende, abgeschlossene und fehlgeschlagene Jobs, Worker online,
  Fortschritt mit Restzeit, aufgehobene Originale (automatisch oder von Hand löschen).
- **Sicher im Heimnetz:**
  - HTTPS mit selbstsigniertem Zertifikat, Zugriff nur aus erlaubten Netzen.
  - Container ohne Root-Rechte mit schreibgeschütztem Dateisystem.
  - Die Container kommunizieren nur über Dateien, nicht über das Netzwerk.

## Aufbau

| Container | Image-Target | Aufgabe | Volumes |
|---|---|---|---|
| `recorder` | `runner` | Twitch prüfen, aufnehmen, VODs laden | `/config`, `/work` |
| `converter` | `converter` (mit ffmpeg) | MP4 erzeugen und in der Mediathek ablegen | `/work`, `/recordings` |
| `web` | `runner`, `python -m app.web` | Weboberfläche und API (HTTPS, Port 8080) | `/config`, `/work` |

Der Recorder läuft auch ohne Webinterface und ohne Konverter weiter. Aufnahmen warten dann in
`/work/queue/`.

## Schnellstart (Docker Compose)

```bash
git clone https://github.com/MatKos169/StreamlinkContainer.git
cd StreamlinkContainer
cp clientid.env.example clientid.env            # TWITCH_CLIENT_ID und TWITCH_CLIENT_SECRET eintragen
mkdir -p config work recordings && sudo chown 1027:100 config work recordings
# in default.env: ALLOWED_NETWORKS (dein Heimnetz) und TLS_HOSTNAMES (Adresse des Hosts) setzen
docker compose up -d --build
```

- Die Twitch-Zugangsdaten bekommst du unter <https://dev.twitch.tv/console/apps>: neue Anwendung,
  beliebige Redirect-URL, Kategorie „Application Integration“.
- Danach `https://<host>:8080` öffnen. Beim ersten Aufruf warnt der Browser vor dem
  selbstsignierten Zertifikat. Den Fingerabdruck vorher vergleichen:
  `docker compose logs web | grep fingerprint`.
- Kanal eintragen, fertig. Die MP4s landen in `recordings/`.

Ohne Compose, jeden Container einzeln mit `docker run`, lokalen oder SMB-Volumes, auf einem oder
mehreren Hosts: siehe [DEPLOYMENT.md](docs/DEPLOYMENT.md#einzelne-container-ohne-compose).

## Dokumentation

| Dokument | Inhalt |
|---|---|
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | Alle Umgebungsvariablen und Einstellungen, alle Funktionen im Detail |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Wie die Container über Dateien zusammenarbeiten, Job-Ablauf, Zeiten, Verhalten bei Ausfällen |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | Aufbau-Beispiele mit Jellyfin und NAS (NFS, SMB), einzelne Container mit `docker run`, HTTPS, Proxmox-LXC, Fehlersuche |
| [app/static/openapi.yaml](app/static/openapi.yaml) | Die HTTP-API als OpenAPI 3.1, im Betrieb auch unter `https://<host>:8080/api/openapi.yaml` |

## Entwicklung

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest -q
```

- Twitch und streamlink sind in den Tests gemockt.
- Die Konverter-Tests nutzen ein echtes ffmpeg, wenn es installiert ist, sonst werden sie
  übersprungen.
- `tests/test_openapi.py` prüft, dass `openapi.yaml` gültig ist und genau den Routen des Servers
  entspricht.

| Pfad | Inhalt |
|---|---|
| `app/recorder.py` | Recorder: Schleife (Konfiguration, Twitch-Check, Nachprüfung, Stream-Limit, VODs, Status) |
| `app/recording.py` | Live-Aufnahme (`Recording`), Kapitel (`ChapterWriter`) |
| `app/vod.py` | VOD-Downloads (getaktetes Laden der Segmente, Aufträge in `/config/downloads`) |
| `app/converter.py` | Konverter: Kapitel und Metadaten, ffmpeg, Ablage in der Mediathek |
| `app/jobs.py` | Warteschlange im Arbeitsordner: Zustände, Übernahme, Lebenszeichen, Übersicht |
| `app/web.py`, `app/static/` | Weboberfläche, API, `openapi.yaml` |
| `app/twitch.py` | Helix-API (Live-Status, VOD-Metadaten) |
| `app/streams.py`, `app/settings.py` | Kanäle und Einstellungen, Umgebungsvariablen |
| `app/naming.py` | Sichere Datei- und Ordnernamen, Ordnerstruktur |
| `app/auth.py` | Prüfung von App-Zugang und OAuth-Token |
| `app/tls.py`, `app/healthcheck.py` | HTTPS-Zertifikat, Healthcheck des Recorders |
| `tests/` | pytest-Tests |

Hinweise für Coding-Agents stehen in [AGENTS.md](AGENTS.md).

## Lizenz

MIT, siehe [LICENSE.MD](LICENSE.MD).
