# Plex Auto-Transcoder

Ein maßgeschneidertes, automatisiertes Skript, das nächtlich Überprüfungen der Film- und Serienbibliothek durchführt und große Videodateien mithilfe der **Intel Quick Sync Video (QSV)** Hardwarebeschleunigung in platzsparendes **HEVC (H.265)** konvertiert.

## 🚀 Voraussetzungen & Installation

Verwende diese simplen Copy-Paste-Befehle, um dieses Projekt auf einem frischen Linux-System (z.B. Debian/Ubuntu) einzurichten. So wird Docker, Python 3, Flask und der nötige GPU-Zugriff für die Hardwarebeschleunigung installiert und aktiviert:

```bash
# 1. Systempakete (Docker, Git, Python Flask & SQLite) installieren
sudo apt update
sudo apt install -y docker.io git python3 python3-flask sqlite3

# 2. Aktuellen Benutzer zu den Systemgruppen (für Docker & Intel CPU-Zugriff) hinzufügen
sudo usermod -aG docker $USER
sudo usermod -aG render $USER
sudo usermod -aG video $USER

# WICHTIG: Nach diesem Schritt kurz vom Server abmelden und erneut anmelden, damit die Rechte greifen!

# 3. Repository klonen und in den Ordner wechseln
git clone https://github.com/fiveBenilu/hevc_autotranscoder.git
cd hevc_autotranscoder

# 4. Optional: Skript direkt testweise starten (Web-Interface startet auf Port 5000)
python3 auto_transcoder.py
```

## 🌟 Features
- **Automatischer Nacht-Modus**: Das Skript läuft selbstständig zwischen 01:00 und 07:00 Uhr, um Serverressourcen tagsüber zu schonen.
- **Intelligente Filterung**: Überspringt bereits in HEVC kodierte Dateien, es sei denn, sie sind größer als 5 GB (dann werden sie zur Platzersparnis erneut komprimiert).
- **Apple-like Web-Dashboard**: Unter `http://<server-ip>:5000` läuft ein minimalistisches Frontend mit Dark/Light-Mode und automatischen (Echtzeit) Statusaktualisierungen per AJAX.
- **SQLite-Tracking**: Alle Fortschritte, Ersparnisse und Laufzeiten werden in einer lokalen Datenbank (`transcoder.db`) getrackt, sodass keine Datei fälschlicherweise doppelt bearbeitet wird.
- **Hardware-Passthrough**: Umgeht eventuelle Fehler bezüglich fehlender proprietärer Linux-Host-Treiber, indem ein `lscr.io/linuxserver/ffmpeg:latest` Docker-Container für den reinen Transkodierungs-Job gespawnt wird (mit `/dev/dri`-Passthrough).

## 📁 Dateistruktur
- `auto_transcoder.py`: Das Herzstück. Beinhaltet die Logik zum Scannen, die Subprocess-Aufrufe für Docker und den integrierten Flask-Webserver inkl. Dashboard HTML/JS.
- `transcoder.db`: Die SQLite-Datenbank für Historie und Status.
- `plex-transcoder.service`: Die Systemd-Service-Datei, die für den automatischen Hintergrundbetrieb sorgt (inklusive Berechtigungen für Hardware-Beschleunigung).

## ⚙️ Wie funktioniert das Hardware-Encoding?
Das System setzt eine entsprechende Intel CPU mit Hardware-Encodierung (Quick Sync) voraus.
Das Skript greift via `/dev/dri/renderD128` auf das Gerät zu.
Um Treiber-Konflikten auf dem Host-System aus dem Weg zu gehen, startet das Skript intern folgenden Docker-Befehl für jede Datei:
```bash
docker run --rm \
  --device=/dev/dri:/dev/dri \
  -v /pfad/zu/deinen/medien:/pfad/zu/deinen/medien \
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
   watch -n 2 'ls -lh /pfad/zu/deinen/medien/*/*/*.hevc.tmp.mkv 2>/dev/null'
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
Sollte das Skript über Systemd eingebunden sein:
- Starten: `sudo systemctl start plex-transcoder`
- Stoppen: `sudo systemctl stop plex-transcoder`
- Neustarten: `sudo systemctl restart plex-transcoder`
- Status: `sudo systemctl status plex-transcoder`

**Wichtig:** Der ausführende User benötigt zur Grafik-Nutzung die ergänzenden Berechtigungsgruppen `render` und `video`.
