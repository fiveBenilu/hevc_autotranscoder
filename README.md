# Plex Auto-Transcoder

Ein maßgeschneidertes, automatisiertes Skript, das nächtlich Überprüfungen der Film- und Serienbibliothek durchführt und große Videodateien mithilfe der **Intel Quick Sync Video (QSV)** Hardwarebeschleunigung in platzsparendes **HEVC (H.265)** konvertiert.

## 🌟 Features
- **Automatischer Nacht-Modus**: Das Skript läuft selbstständig zwischen 01:00 und 07:00 Uhr, um Serverressourcen tagsüber zu schonen.
- **Intelligente Filterung**: Überspringt bereits in HEVC kodierte Dateien, es sei denn, sie sind größer als 5 GB (dann werden sie zur Platzersparnis erneut komprimiert).
- **Apple-like Web-Dashboard**: Unter `http://<server-ip>:5000` läuft ein minimalistisches Frontend mit Dark/Light-Mode und automatischen (Echtzeit) Statusaktualisierungen per AJAX.
- **SQLite-Tracking**: Alle Fortschritte, Ersparnisse und Laufzeiten werden in einer lokalen Datenbank (`transcoder.db`) getrackt, sodass keine Datei fälschlicherweise doppelt bearbeitet wird.
- **Hardware-Passthrough**: Umgeht fehlende proprietäre Debian 13 Treiber (Trixie), indem ein `lscr.io/linuxserver/ffmpeg:latest` Docker-Container für den reinen Transkodierungs-Job gespawnt wird (mit `/dev/dri`-Passthrough).

## 📁 Dateistruktur (in `/home/bennetgriese/plex/transcoder/`)
- `auto_transcoder.py`: Das Herzstück. Beinhaltet die Logik zum Scannen, die Subprocess-Aufrufe für Docker und den integrierten Flask-Webserver inkl. Dashboard HTML/JS.
- `transcoder.db`: Die SQLite-Datenbank für Historie und Status.
- `plex-transcoder.service`: Die Systemd-Service-Datei, die für den automatischen Hintergrundbetrieb sorgt (inklusive Berechtigungen für Hardware-Beschleunigung).

## ⚙️ Wie funktioniert das Hardware-Encoding?
Dein **Intel i5 8500T** (UHD 630) hat Hardware-Encodierung verbaut.
Das Skript greift via `/dev/dri/renderD128` auf das Gerät zu.
Da das lokale Debian gelegentlich Hürden bezüglich proprietärer Codecs aufweist, startet das Skript intern folgenden Docker-Befehl für jede Datei:
```bash
docker run --rm \
  --device=/dev/dri:/dev/dri \
  -v /home/bennetgriese/plex/media:/home/bennetgriese/plex/media \
  lscr.io/linuxserver/ffmpeg:latest \
  -y -vaapi_device /dev/dri/renderD128 \
  -i <INPUT> -vf format=nv12,hwupload -c:v hevc_vaapi \
  -global_quality 23 -c:a copy -c:s copy <OUTPUT.hevc.tmp.mkv>
```
So werden Video-Spuren extrem hardware-effizient komprimiert, während Ton und Untertitel unangetastet kopiert (`copy`) werden.

## 🔍 Überwachung & Status prüfen

Neben dem Web-Dashboard kannst du jederzeit über die Kommandozeile nachschauen, was unter der Haube vor sich geht – *ohne* den Prozess abzubrechen:

1. **Größe der temporären Datei (Echtzeit) beobachten:**
   ```bash
   watch -n 2 'ls -lh /home/bennetgriese/plex/media/*/*/*.hevc.tmp.mkv 2>/dev/null'
   ```
2. **Log-Ausgabe des Skripts (Live-Verfolgung):**
   ```bash
   sudo journalctl -u plex-transcoder.service -f
   ```
3. **Hardware-Auslastung des FFMPEG-Containers:**
   ```bash
   docker stats
   ```

## 🛠️ Systemd Dienstverwaltung
Solltest du das Skript stoppen oder neustarten wollen:
- Starten: `sudo systemctl start plex-transcoder`
- Stoppen: `sudo systemctl stop plex-transcoder`
- Neustarten: `sudo systemctl restart plex-transcoder`
- Status: `sudo systemctl status plex-transcoder`

**Wichtig:** Der Dienst läuft unter dem User `bennetgriese` und hat zur Grafik-Nutzung die ergänzenden Berechtigungsgruppen `render` und `video` hinterlegt.
