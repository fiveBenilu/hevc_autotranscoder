# Plex Auto-Transcoder

A custom, automated script that runs nightly checks on your movie and TV show library, converting large video files into space-saving **HEVC (H.265)** using **Intel Quick Sync Video (QSV)** hardware acceleration.

## 🚀 Prerequisites & Installation

Use these simple copy-paste commands to set up this project on a fresh Linux system (e.g., Debian/Ubuntu). This will install Docker, Python 3, Flask, and enable the necessary GPU access for hardware acceleration:

```bash
# 1. Install system packages (Docker, Git, Python Flask & SQLite)
sudo apt update
sudo apt install -y docker.io git python3 python3-flask sqlite3

# 2. Add current user to system groups (for Docker & Intel CPU access)
sudo usermod -aG docker $USER
sudo usermod -aG render $USER
sudo usermod -aG video $USER

# IMPORTANT: Log out and log back in for the new group permissions to take effect!

# 3. Clone the repository and navigate into the folder
git clone https://github.com/fiveBenilu/hevc_autotranscoder.git
cd hevc_autotranscoder

# 4. Optional: Run the script directly for testing (Web interface starts on port 5000)
python3 auto_transcoder.py
```

## 🌟 Features
- **Automatic Night Mode**: The script runs autonomously between 01:00 and 07:00 AM to conserve server resources during the day.
- **Smart Filtering**: Skips files already encoded in HEVC unless they are larger than 5 GB (then they are re-encoded to save space).
- **Apple-like Web Dashboard**: A minimalist frontend with Dark/Light mode and real-time status updates via AJAX is available at `http://<server-ip>:5000`.
- **SQLite Tracking**: All progress, savings, and runtimes are tracked in a local database (`transcoder.db`), preventing accidental duplicate processing.
- **Hardware Passthrough**: Bypasses missing proprietary Linux host drivers by spawning an `lscr.io/linuxserver/ffmpeg:latest` Docker container for the transcoding job (with `/dev/dri` passthrough).

## 📁 File Structure
- `auto_transcoder.py`: The core script. Contains the scanning logic, subprocess calls to Docker, and the integrated Flask web server including the Dashboard HTML/JS.
- `transcoder.db`: The SQLite database for history and status.
- `plex-transcoder.service`: The systemd service file ensuring automatic background operation (includes hardware acceleration permissions).

## ⚙️ How does Hardware-Encoding work?
The system requires a compatible Intel CPU with hardware encoding capabilities (Quick Sync).
The script accesses the device via `/dev/dri/renderD128`.
To avoid driver conflicts on the host system, the script runs the following Docker command internally for each file:
```bash
docker run --rm \
  --device=/dev/dri:/dev/dri \
  -v /path/to/your/media:/path/to/your/media \
  lscr.io/linuxserver/ffmpeg:latest \
  -y -vaapi_device /dev/dri/renderD128 \
  -i <INPUT> -vf format=nv12,hwupload -c:v hevc_vaapi \
  -global_quality 23 -c:a copy -c:s copy <OUTPUT.hevc.tmp.mkv>
```
This compresses video tracks extremely efficiently via hardware, while audio and subtitles are simply copied unmodified (`copy`).

## 🔍 Monitoring & Status Checks

Besides the web dashboard, you can always check what's going on under the hood via the command line – *without* interrupting the process:

1. **Watch the temporary file size in real-time:**
   ```bash
   watch -n 2 'ls -lh /path/to/your/media/*/*/*.hevc.tmp.mkv 2>/dev/null'
   ```
2. **Live script logs:**
   ```bash
   sudo journalctl -u plex-transcoder.service -f
   ```
3. **Hardware usage of the FFMPEG container:**
   ```bash
   docker stats
   ```

## 🛠️ Systemd Service Management
If the script is set up via systemd:
- Start: `sudo systemctl start plex-transcoder`
- Stop: `sudo systemctl stop plex-transcoder`
- Restart: `sudo systemctl restart plex-transcoder`
- Status: `sudo systemctl status plex-transcoder`

**Important:** The executing user requires the supplementary permission groups `render` and `video` for GPU access.
