# Deployment-Beispiele mit Jellyfin und NAS

Drei typische Aufbauten, vom einfachsten zum verteilten. Alle Beispiele gehen davon aus, dass das
Repository auf dem jeweiligen Docker-Host liegt
(`git clone https://github.com/MatKos169/StreamlinkContainer.git`). Die Images werden dort mit
`docker compose build` gebaut. Variablen und Funktionen: [CONFIGURATION.md](CONFIGURATION.md).
Wie die Container zusammenarbeiten: [ARCHITECTURE.md](ARCHITECTURE.md).

- [Grundsätze](#grundsätze)
- [Beispiel 1: Ein Docker-Host, NAS als Mediathek](#beispiel-1-ein-docker-host-nas-als-mediathek)
- [Beispiel 2: Alles auf einer Synology](#beispiel-2-alles-auf-einer-synology)
- [Beispiel 3: Verteilt mit mehreren Konvertern](#beispiel-3-verteilt-mit-mehreren-konvertern)
- [Jellyfin einrichten](#jellyfin-einrichten)
- [Proxmox-LXC](#proxmox-lxc)
- [Prüfen und Fehlersuche](#prüfen-und-fehlersuche)

## Grundsätze

| Volume | Wohin | Warum |
|---|---|---|
| `/config` | lokal auf dem Host von recorder und web | Klein, wird jede Sekunde gelesen |
| `/work` | **lokal (SSD), wenn alle Konverter auf demselben Host laufen**, sonst eine gemeinsame NFS-Freigabe | Hier wird live geschrieben. Bricht die Verbindung zum NAS ab, bricht die Aufnahme ab. Lokal ist robuster und schneller |
| `/recordings` | NAS-Freigabe der Mediathek, die Jellyfin liest | Endablage der MP4s. Wird nur vom Konverter geschrieben, Jellyfin braucht nur Lesezugriff |

- **Platzbedarf in `/work`:** eine Twitch-Stunde in 1080p60 sind etwa 3–4 GB. Rechne mit
  gleichzeitige Aufnahmen × längster Stream, plus der Warteschlange, plus `done/`, falls die
  Originale aufgehoben werden.
- **Rechte:** Alle Container laufen als `PUID:PGID` (Standard `1027:100`). Diese UID/GID muss auf
  `config`, `work` und `recordings` schreiben dürfen, bei NFS auch auf dem NAS.
- `/work` und `/recordings` dürfen auf verschiedenen Dateisystemen liegen. Der Konverter kopiert
  dann unter einem versteckten Namen und benennt erst am Ende um, Jellyfin sieht also nie eine
  halbe Datei. **Innerhalb** von `/work` muss alles auf einem Dateisystem liegen, also immer
  `/work` als Ganzes einhängen.
- Zugangsdaten in `clientid.env`:
  ```bash
  cp clientid.env.example clientid.env   # TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET eintragen
  ```

## Beispiel 1: Ein Docker-Host, NAS als Mediathek

Ein Server (Mini-PC, VM, Proxmox-LXC) betreibt recorder, converter, web **und Jellyfin**. Das
NAS stellt nur die Mediathek per NFS bereit.

```
┌──────────── Docker-Host ────────────┐          ┌──── NAS ────┐
│ recorder ─► /srv/streamlink/work    │          │             │
│ converter ─► /mnt/nas/media/Twitch ─┼── NFS ──►│ /media      │
│ web :8080                           │          │  └ Twitch/  │
│ jellyfin :8096 ◄─ /mnt/nas/media    │          │             │
└─────────────────────────────────────┘          └─────────────┘
```

NFS-Freigabe auf dem Host einhängen (`/etc/fstab`):

```fstab
nas.local:/volume1/media  /mnt/nas/media  nfs4  rw,hard,noatime,_netdev  0 0
```

```bash
sudo mkdir -p /mnt/nas/media /srv/streamlink/{config,work}
sudo mount /mnt/nas/media
sudo mkdir -p /mnt/nas/media/Twitch
sudo chown 1027:100 /srv/streamlink/config /srv/streamlink/work /mnt/nas/media/Twitch
```

`.env` neben der `docker-compose.yml` des Repos:

```env
PUID=1027
PGID=100
CONFIG_PATH=/srv/streamlink/config
WORK_PATH=/srv/streamlink/work
RECORDINGS_PATH=/mnt/nas/media/Twitch
WEB_PORT=8080
```

In `default.env` das eigene Netz erlauben und die Adresse des Hosts ins Zertifikat aufnehmen, z. B.
`ALLOWED_NETWORKS=192.168.1.0/24` und `TLS_HOSTNAMES=192.168.1.10,dockerhost.local`.

Jellyfin dazu, z. B. als `docker-compose.override.yml` im Repo-Ordner (wird von Compose
automatisch mitgeladen):

```yaml
services:
  jellyfin:
    image: jellyfin/jellyfin:latest
    container_name: jellyfin
    user: "1027:100"
    restart: unless-stopped
    ports:
      - "8096:8096"
    volumes:
      - /srv/jellyfin/config:/config
      - /srv/jellyfin/cache:/cache
      - /mnt/nas/media/Twitch:/media/twitch:ro   # nur lesen
```

```bash
docker compose up -d --build
```

Danach den Kanal im Webinterface (`https://<host>:8080`, beim ersten Aufruf die Warnung zum
selbstsignierten Zertifikat bestätigen, siehe [HTTPS](#https-und-zertifikat)) mit einem Ausgabeordner wie `Name`
anlegen. Bei der Ordnerstruktur `output_year_month` entsteht
`/mnt/nas/media/Twitch/Name-2026/11/2026-11-06 20-15-00 - Titel.mp4`.

Mehr Konverter auf demselben Host: `docker compose up -d --scale converter=2`.

## Beispiel 2: Alles auf einer Synology

Das NAS selbst betreibt die Container (Container Manager oder `docker compose` per SSH) und
Jellyfin. So war auch das Original-Projekt gedacht (`1027:100` = erster Admin und Gruppe `users`).

```
┌──────────────── Synology ────────────────┐
│ recorder ─► /volume1/docker/streamlink/work
│ converter ─► /volume1/video/Twitch        │
│ web :8080                                 │
│ jellyfin :8096 ◄─ /volume1/video          │
└───────────────────────────────────────────┘
```

```bash
# per SSH auf der Synology
sudo mkdir -p /volume1/docker/streamlink/{config,work} /volume1/video/Twitch
sudo chown -R 1027:100 /volume1/docker/streamlink /volume1/video/Twitch
cd /volume1/docker/streamlink && git clone https://github.com/MatKos169/StreamlinkContainer.git app
cd app && cp clientid.env.example clientid.env   # Zugangsdaten eintragen
```

`.env`:

```env
PUID=1027
PGID=100
CONFIG_PATH=/volume1/docker/streamlink/config
WORK_PATH=/volume1/docker/streamlink/work
RECORDINGS_PATH=/volume1/video/Twitch
WEB_PORT=8080
```

```bash
sudo docker compose up -d --build      # ältere DSM-Versionen: sudo docker-compose up -d --build
```

Hinweise:
- **Mit Normalisierung und `copy`** wird nur das Audio neu kodiert. Das schaffen auch kleine
  Synology-CPUs.
- **Neukodierung des Videos** (`libx264`, `libx265`) ist auf den meisten NAS-CPUs sehr langsam. Dann
  besser einen zusätzlichen Konverter auf einem stärkeren Rechner betreiben (Beispiel 3). Der
  braucht dafür eine NFS-Freigabe von `/volume1/docker/streamlink/work`.
- Läuft auf Port 8080 schon etwas (z. B. ein anderes Paket), `WEB_PORT` ändern.
- In Jellyfin den Ordner `/volume1/video/Twitch` als Bibliothek einbinden.

## Beispiel 3: Verteilt mit mehreren Konvertern

- Ein kleiner, ständig laufender Host (Raspberry Pi, LXC) nimmt auf: recorder und web.
- Die Umwandlung übernehmen ein oder mehrere stärkere Rechner, z. B. der Jellyfin-Server und ein
  Desktop-PC, wenn er an ist.
- Das NAS hält Arbeitsordner und Mediathek.

```
                 ┌──────────────── NAS ────────────────┐
                 │ /volume1/streamlink-work  (NFS)     │
                 │ /volume1/media/Twitch     (NFS)     │
                 └───▲─────────────▲──────────────▲────┘
                     │ work        │ work + media │ work + media
┌──── Host A (Pi) ───┴─┐  ┌─ Host B (Jellyfin) ───┴┐  ┌─ Host C (PC) ──────┐
│ recorder, web :8080  │  │ converter "jellyfin"   │  │ converter "pc"     │
│ /config lokal        │  │ jellyfin :8096         │  │ (läuft nur, wenn   │
└──────────────────────┘  └────────────────────────┘  │  der PC an ist)    │
                                                      └────────────────────┘
```

Hier muss `/work` auf dem NAS liegen, weil alle Hosts denselben Arbeitsordner sehen müssen. Die
Konverter verteilen die Jobs selbst (siehe [ARCHITECTURE.md](ARCHITECTURE.md#der-arbeitsordner-work)):
- Ein Konverter, der ausgeschaltet wird, gibt seinen Job zurück in die Warteschlange.
- Ein abgestürzter Konverter verliert seinen Job nach 10 Minuten an einen anderen.

**NAS:** zwei NFS-Freigaben (`streamlink-work`, `media`) für die IPs der drei Hosts, Schreibrecht
für UID `1027`. Auf Synology: NFS-Regel mit „Squash: Keine Zuordnung“. Die Ordner gehören
`1027:100`.

**Host A** (recorder + web, `/config` lokal, `/work` per NFS). `/etc/fstab`:

```fstab
nas.local:/volume1/streamlink-work  /mnt/streamlink-work  nfs4  rw,hard,noatime,_netdev  0 0
```

`.env`:

```env
CONFIG_PATH=/srv/streamlink/config
WORK_PATH=/mnt/streamlink-work
```

```bash
docker compose up -d --build recorder web    # nur diese beiden Dienste
```

**Host B und C** (nur Konverter). Statt fester Mounts können die NFS-Freigaben auch direkt als
Docker-Volumes eingebunden werden. Dann ist auf dem Host nichts in `/etc/fstab` nötig. Eigene
Datei, z. B. `docker-compose.nfs-converter.yml` im Repo-Ordner:

```yaml
services:
  converter:
    build:
      context: .
      target: converter
    image: streamlink-converter:local
    user: "1027:100"
    read_only: true
    tmpfs: [/tmp]
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]
    restart: unless-stopped
    env_file: [default.env]
    environment:
      CONVERTER_NAME: ${CONVERTER_NAME:?Namen setzen, z. B. jellyfin oder pc}
    volumes:
      - work:/work
      - media:/recordings
    stop_grace_period: 40s

volumes:
  work:
    driver: local
    driver_opts:
      type: nfs
      o: "addr=nas.local,nfsvers=4.1,rw,hard,noatime"
      device: ":/volume1/streamlink-work"
  media:
    driver: local
    driver_opts:
      type: nfs
      o: "addr=nas.local,nfsvers=4.1,rw,hard,noatime"
      device: ":/volume1/media/Twitch"
```

```bash
CONVERTER_NAME=jellyfin docker compose -f docker-compose.nfs-converter.yml up -d --build   # Host B
CONVERTER_NAME=pc       docker compose -f docker-compose.nfs-converter.yml up -d --build   # Host C
```

Alternativ mit festen Mounts: `docker-compose.converter.yml` aus dem Repo mit
`WORK_PATH`/`RECORDINGS_PATH`.

Host B betreibt außerdem Jellyfin wie in Beispiel 1, mit der Freigabe `media` schreibgeschützt.

Tipp: Wenn auf Host C neu kodiert werden soll (`libx265`), die Einstellung im Webinterface
setzen. Sie gilt für alle Konverter, also braucht jeder Konverter-Host genug CPU, sonst bleiben
die Jobs lange liegen. Für gemischte Hosts lieber `copy` lassen.

## Jellyfin einrichten

1. **Bibliothek hinzufügen** mit dem Ordner der Aufnahmen (z. B. `/media/twitch`). Als Inhaltstyp
   eignet sich **„Heimvideos und Fotos“**: Jellyfin sucht dann keine Film-Metadaten im Internet,
   die bei Stream-Titeln zu falschen Treffern führen würden.
2. **Eingebettete Titel:** Der Konverter schreibt den Stream-Titel als `title` in die MP4. Wenn
   Jellyfin diesen statt des Dateinamens anzeigen soll, die Option für eingebettete Titel in den
   Bibliothekseinstellungen aktivieren. Ihr Name kann je nach Jellyfin-Version etwas abweichen.
3. **Kapitel:** Die Kategorien sind als MP4-Kapitel eingebettet und erscheinen beim Abspielen in
   der Kapitelauswahl. Mit „Kapitelbilder extrahieren“ in den Bibliothekseinstellungen bekommen
   sie Vorschaubilder. Das kostet beim Scannen etwas CPU.
4. **Ordnerstruktur:** `output_year_month` (`Name-2026/11/`) oder `year_month` (`Name/2026/11/`)
   ergeben pro Kanal Ordner nach Jahr und Monat. Die Dateinamen beginnen mit Datum und Uhrzeit
   und sortieren sich daher richtig.
5. **Echtzeitüberwachung** der Bibliothek kann auf NFS unzuverlässig sein, weil
   inotify-Ereignisse über NFS meist nicht ankommen. Dann einen geplanten Bibliotheksscan
   einstellen, z. B. stündlich.

## Proxmox-LXC

- **Unprivilegierte LXC-Container** können keine NFS-Freigaben selbst einhängen. Stattdessen die
  Freigabe auf dem Proxmox-Host einhängen und per Bind-Mount in den Container geben:
  ```bash
  # auf dem Proxmox-Host
  pct set <ctid> -mp0 /mnt/pve/nas-media,mp=/mnt/nas/media
  ```
- **UID-Verschiebung:** In unprivilegierten Containern ist UID 1027 im Container auf dem Host
  UID 101027. Dateien auf dem NAS gehören dann 101027. Entweder auf dem NAS dafür Rechte
  vergeben, ein `lxc.idmap` für 1027 einrichten oder `PUID`/`PGID` passend wählen.
- **Docker in LXC** braucht `nesting=1` (und `keyctl=1`) in den Optionen des Containers.

## HTTPS und Zertifikat

- Das Webinterface läuft immer per HTTPS: `https://<host>:<WEB_PORT>`. Beim ersten Start erzeugt
  es ein selbstsigniertes Zertifikat in `config/tls/`.
- **Erster Aufruf:** Der Browser warnt vor dem unbekannten Zertifikat. Vorher den Fingerabdruck
  prüfen:
  ```bash
  docker compose logs web | grep fingerprint
  ```
  Er muss mit dem übereinstimmen, den der Browser in den Zertifikatsdetails zeigt. Dann die
  Ausnahme bestätigen.
- **Ohne Warnung:** `config/tls/cert.pem` auf den Geräten als vertrauenswürdig importieren. Die
  Datei ist öffentlich, `key.pem` dagegen nie weitergeben.
- **Hostnamen:** Die Adresse, unter der das Webinterface aufgerufen wird, gehört in
  `TLS_HOSTNAMES`, sonst passt das Zertifikat nicht zur Adresse.
- **Eigenes Zertifikat**, z. B. von einer eigenen CA:
  ```env
  TLS_CERT_FILE=/config/tls/own-cert.pem
  TLS_KEY_FILE=/config/tls/own-key.pem
  ```
- **Hinter einem Reverse Proxy** (Nginx Proxy Manager, Traefik, Caddy):
  - Entweder macht der Proxy HTTPS zum Webinterface (Backend `https://`, Prüfung des
    selbstsignierten Zertifikats im Proxy abschalten oder `cert.pem` hinterlegen).
  - Oder `WEB_TLS=false`. Dann aber nur, wenn das Webinterface ausschließlich vom Proxy
    erreichbar ist, also ohne veröffentlichten Port.
  - In beiden Fällen gehört die IP des Proxys in `ALLOWED_NETWORKS`.

## Prüfen und Fehlersuche

```bash
docker compose ps                         # recorder sollte "healthy" sein
docker compose logs -f recorder           # Twitch-Checks, Aufnahmen, Downloads
docker compose logs -f converter          # Umwandlungen
ls /srv/streamlink/work/{recording,queue,converting,failed}
```

| Symptom | Ursache / Lösung |
|---|---|
| Webinterface antwortet `Forbidden` | IP nicht in `ALLOWED_NETWORKS`. Im Log von `web` steht `Rejected request from <ip>` |
| Browser: „Verbindung wurde zurückgesetzt“ / leere Seite | Mit `http://` statt `https://` aufgerufen |
| Browser meldet falschen Namen im Zertifikat | Adresse in `TLS_HOSTNAMES` aufnehmen, Web-Container neu starten (das Zertifikat wird dann neu erzeugt) |
| „Twitch-Abfrage fehlgeschlagen: TwitchAuthorizationException (HTTP 400)“ | `TWITCH_CLIENT_ID`/`SECRET` falsch |
| „Worker online: 0“, Jobs bleiben in „wartet auf Konverter“ | Kein Konverter läuft, oder er sieht nicht dasselbe `/work`. Auf jedem Host prüfen, ob `ls /work/queue` dieselben Jobs zeigt |
| Jobs landen in `failed/` mit `PermissionError` | `PUID`/`PGID` hat keine Schreibrechte auf `/recordings` bzw. die NFS-Freigabe (UID-Zuordnung prüfen) |
| Aufnahmen brechen bei NAS-Aussetzern ab | `/work` auf lokalen Speicher legen (Beispiel 1/2) oder NFS mit `hard` einhängen |
| Jellyfin zeigt neue Dateien erst spät | Geplanten Bibliotheksscan einrichten (siehe oben) |
