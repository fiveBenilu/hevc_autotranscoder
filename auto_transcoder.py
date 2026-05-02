import os
import glob
import subprocess
import sqlite3
import time
import threading
import json
import re
from datetime import datetime, timedelta
from flask import Flask, render_template, jsonify, request, redirect

app = Flask(__name__, template_folder='templates', static_folder='static')

DB_FILE = "transcoder.db"
ALLOWED_EXTENSIONS = ('.mkv', '.mp4', '.avi', '.mov')
MAX_TRANSCODE_RETRIES = 2
RETRY_BACKOFF_SECONDS = 8

# Events & state
force_scan_event = threading.Event()
is_scanning = False
current_process = None
transcode_progress = {}
cancel_requested = False
skip_current_requested = False

def get_sys_stats():
    # RAM calculation
    mem_total = 0
    mem_available = 0
    try:
        with open('/proc/meminfo', 'r') as f:
            for line in f:
                if line.startswith('MemTotal:'):
                    mem_total = int(line.split()[1]) * 1024
                elif line.startswith('MemAvailable:'):
                    mem_available = int(line.split()[1]) * 1024
    except:
        pass
        
    ram_str = "N/A"
    if mem_total > 0:
        ram_pct = round(100 - (mem_available / mem_total * 100), 1)
        ram_str = f"{ram_pct}% ({(mem_total - mem_available)//1024//1024}MB / {mem_total//1024//1024}MB)"

    # CPU Load
    try:
        load1, load5, load15 = os.getloadavg()
        cpu_str = f"{round(load1,2)}"
    except:
        cpu_str = "N/A"
        
    # CPU Temp (Try hardware sensors first, fallback to generic thermal zone)
    temp_str = "N/A"
    try:
        found_temp = False
        for hwmon in glob.glob('/sys/class/hwmon/hwmon*'):
            try:
                with open(os.path.join(hwmon, 'name'), 'r') as f:
                    if 'coretemp' in f.read():
                        with open(os.path.join(hwmon, 'temp1_input'), 'r') as f:
                            temp = float(f.read().strip()) / 1000
                            temp_str = f"{round(temp, 1)}°C"
                            found_temp = True
                        break
            except:
                continue
        
        if not found_temp:
            with open('/sys/class/thermal/thermal_zone0/temp', 'r') as f:
                temp = float(f.read().strip()) / 1000
                temp_str = f"{round(temp, 1)}°C"
    except:
        pass

    return cpu_str, temp_str, ram_str

def get_storage_stats():
    drives = []
    try:
        output = subprocess.check_output(['df', '-B1'], text=True)
        lines = output.strip().split('\n')[1:]
        for line in lines:
            parts = line.split()
            if len(parts) >= 6:
                fs, total, used, free, perc = parts[0], int(parts[1]), int(parts[2]), int(parts[3]), parts[4]
                mount = " ".join(parts[5:])
                if fs.startswith('/dev/') and not '/loop' in fs:
                    drives.append({
                        "mount": mount,
                        "fs": fs,
                        "total": format_size(total),
                        "used": format_size(used),
                        "free": format_size(free),
                        "perc": perc
                    })
    except:
        pass
    return drives

def format_size(size_bytes):
    if not size_bytes:
        return "-"
    if size_bytes >= 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"
    elif size_bytes >= 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.2f} MB"
    else:
        return f"{size_bytes / 1024:.2f} KB"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS conversions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT,
            filepath TEXT UNIQUE,
            old_size_bytes INTEGER,
            new_size_bytes INTEGER,
            status TEXT,
            error_log TEXT,
            started_at DATETIME,
            finished_at DATETIME,
            attempt_count INTEGER DEFAULT 0
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS directories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT UNIQUE
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('quality', '23')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('scan_start_hour', '1')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('scan_end_hour', '7')")
    c.execute("PRAGMA table_info(conversions)")
    existing_columns = {row[1] for row in c.fetchall()}
    if 'attempt_count' not in existing_columns:
        c.execute("ALTER TABLE conversions ADD COLUMN attempt_count INTEGER DEFAULT 0")
    c.execute("SELECT COUNT(*) FROM directories")
    if c.fetchone()[0] == 0:
        for d in ["/home/bennetgriese/plex/media/movies", "/home/bennetgriese/plex/media/tv"]:
            if os.path.exists(d):
                c.execute("INSERT INTO directories (path) VALUES (?)", (d,))
    conn.commit()
    conn.close()

def get_settings():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT key, value FROM settings")
    settings_dict = {row[0]: row[1] for row in c.fetchall()}
    q = settings_dict.get('quality', '23')
    start_hr = int(settings_dict.get('scan_start_hour', '1'))
    end_hr = int(settings_dict.get('scan_end_hour', '7'))
    
    c.execute("SELECT id, path FROM directories")
    dirs = [{"id": r[0], "path": r[1]} for r in c.fetchall()]
    conn.close()
    return {"quality": q, "scan_start_hour": start_hr, "scan_end_hour": end_hr, "directories": dirs}

def is_night_time():
    s = get_settings()
    hour = datetime.now().hour
    start_hr = s["scan_start_hour"]
    end_hr = s["scan_end_hour"]
    if start_hr < end_hr:
        return start_hr <= hour < end_hr
    else: # e.g. start at 22, end at 6 (overnight)
        return hour >= start_hr or hour < end_hr

def get_video_duration(filepath):
    try:
        cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", filepath]
        return float(subprocess.check_output(cmd, text=True).strip())
    except:
        return 0.0

def get_video_codec(filepath):
    try:
        cmd = [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=codec_name", "-of",
            "default=noprint_wrappers=1:nokey=1", filepath
        ]
        codec = subprocess.check_output(cmd, text=True).strip()
        return codec
    except:
        return None

def is_transcode_tempfile(filepath):
    return os.path.basename(filepath).endswith('.hevc.tmp.mkv')

def cleanup_transcode_tempfile(filepath):
    if is_transcode_tempfile(filepath) and os.path.exists(filepath):
        try:
            os.remove(filepath)
            return True
        except:
            return False
    return False

def parse_db_datetime(value):
    if not value:
        return None

    if isinstance(value, datetime):
        return value

    try:
        return datetime.fromisoformat(str(value))
    except:
        return None

def format_duration(seconds):
    if seconds is None:
        return "-"

    total_seconds = int(round(seconds))
    if total_seconds < 0:
        return "-"

    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)

    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"

def format_percent(value):
    if value is None:
        return "-"
    return f"{round(value, 1)}%"

def get_stats_report():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        SELECT filename, filepath, status, old_size_bytes, new_size_bytes,
               attempt_count, started_at, finished_at, error_log
        FROM conversions
        ORDER BY id ASC
    """)
    rows = c.fetchall()
    conn.close()

    now = datetime.now()
    seven_days = [now.date() - timedelta(days=offset) for offset in range(6, -1, -1)]
    daily = {
        day.isoformat(): {
            "label": day.strftime("%a"),
            "date": day.isoformat(),
            "completed": 0,
            "failed": 0,
            "skipped": 0,
            "perma_skipped": 0,
            "cancelled": 0,
            "retrying": 0,
            "saved_bytes": 0,
        }
        for day in seven_days
    }

    totals = {
        "completed": 0,
        "failed": 0,
        "skipped": 0,
        "perma_skipped": 0,
        "cancelled": 0,
        "retrying": 0,
    }

    last_24h = {
        "completed": 0,
        "failed": 0,
        "skipped": 0,
        "perma_skipped": 0,
        "cancelled": 0,
        "saved_bytes": 0,
    }

    total_saved_bytes = 0
    total_attempts = 0
    retried_jobs = 0
    completed_durations = []
    top_savings = []
    error_groups = {}

    cutoff_24h = now - timedelta(hours=24)

    for row in rows:
        filename, filepath, status, old_size, new_size, attempt_count, started_at, finished_at, error_log = row
        old_size = old_size or 0
        new_size = new_size or 0
        attempt_count = attempt_count or 0
        status_key = status.lower() if status else None
        started_dt = parse_db_datetime(started_at)
        finished_dt = parse_db_datetime(finished_at)
        saved_bytes = max(old_size - new_size, 0) if status == 'COMPLETED' else 0

        if status_key in totals:
            totals[status_key] += 1

        if status == 'COMPLETED':
            total_saved_bytes += saved_bytes
            total_attempts += max(attempt_count, 1)
            if attempt_count > 1:
                retried_jobs += 1

            if started_dt and finished_dt:
                duration_seconds = (finished_dt - started_dt).total_seconds()
                if duration_seconds >= 0:
                    completed_durations.append(duration_seconds)

            top_savings.append({
                "filename": filename,
                "filepath": filepath,
                "saved_bytes": saved_bytes,
                "saved": format_size(saved_bytes),
                "attempts": attempt_count or 1,
                "duration": format_duration((finished_dt - started_dt).total_seconds()) if started_dt and finished_dt else "-",
            })

        if finished_dt:
            date_key = finished_dt.date().isoformat()
            if date_key in daily:
                if status in ('COMPLETED', 'FAILED', 'SKIPPED', 'PERMA_SKIPPED', 'CANCELLED', 'RETRYING'):
                    daily[date_key][status.lower()] += 1
                daily[date_key]["saved_bytes"] += saved_bytes

            if finished_dt >= cutoff_24h:
                if status == 'COMPLETED':
                    last_24h["completed"] += 1
                    last_24h["saved_bytes"] += saved_bytes
                elif status == 'FAILED':
                    last_24h["failed"] += 1
                elif status == 'SKIPPED':
                    last_24h["skipped"] += 1
                elif status == 'PERMA_SKIPPED':
                    last_24h["perma_skipped"] += 1
                elif status == 'CANCELLED':
                    last_24h["cancelled"] += 1

        if status == 'FAILED' and error_log:
            error_key = error_log.strip().splitlines()[0][:140]
            error_groups[error_key] = error_groups.get(error_key, 0) + 1

    top_savings.sort(key=lambda item: item["saved_bytes"], reverse=True)
    top_savings = top_savings[:5]

    top_errors = sorted(error_groups.items(), key=lambda item: item[1], reverse=True)[:5]
    top_errors = [{"message": message, "count": count} for message, count in top_errors]

    total_items = sum(totals.values())
    success_rate = (totals["completed"] / total_items * 100) if total_items else 0
    retry_rate = (retried_jobs / totals["completed"] * 100) if totals["completed"] else 0
    avg_attempts = (total_attempts / totals["completed"]) if totals["completed"] else 0
    avg_duration = (sum(completed_durations) / len(completed_durations)) if completed_durations else 0
    avg_saved = (total_saved_bytes / totals["completed"]) if totals["completed"] else 0

    daily_series = []
    for day in seven_days:
        key = day.isoformat()
        entry = daily[key]
        entry_total = entry["completed"] + entry["failed"] + entry["skipped"] + entry["perma_skipped"] + entry["cancelled"]
        daily_series.append({
            **entry,
            "total": entry_total,
            "saved": format_size(entry["saved_bytes"]),
        })

    return {
        "totals": totals,
        "saved_bytes": total_saved_bytes,
        "saved": format_size(total_saved_bytes),
        "processed": totals["completed"],
        "skipped": totals["skipped"],
        "failed": totals["failed"],
        "perma_skipped": totals["perma_skipped"],
        "cancelled": totals["cancelled"],
        "retrying": totals["retrying"],
        "retried_jobs": retried_jobs,
        "success_rate": format_percent(success_rate),
        "retry_rate": format_percent(retry_rate),
        "avg_attempts": round(avg_attempts, 2),
        "avg_duration_seconds": round(avg_duration, 2),
        "avg_duration": format_duration(avg_duration),
        "avg_saved_bytes": round(avg_saved, 2),
        "avg_saved": format_size(avg_saved),
        "last_24h": {
            **last_24h,
            "saved": format_size(last_24h["saved_bytes"]),
        },
        "daily": daily_series,
        "top_savings": top_savings,
        "top_errors": top_errors,
    }

def is_transient_transcode_error(message):
    if not message:
        return False

    transient_patterns = (
        "resource temporarily unavailable",
        "device or resource busy",
        "connection reset by peer",
        "connection timed out",
        "broken pipe",
        "i/o error",
        "cannot allocate memory",
        "temporary failure",
        "could not open output file",
        "error while opening",
    )

    normalized = message.lower()
    return any(pattern in normalized for pattern in transient_patterns)

def process_file(filepath, quality):
    global current_process, transcode_progress, cancel_requested, skip_current_requested

    if is_transcode_tempfile(filepath):
        cleanup_transcode_tempfile(filepath)
        return
    
    filename = os.path.basename(filepath)
    try:
        old_size = os.path.getsize(filepath)
    except:
        return
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    c.execute("SELECT status FROM conversions WHERE filepath=?", (filepath,))
    row = c.fetchone()
    
    if row and row[0] in ('COMPLETED', 'IN_PROGRESS', 'SKIPPED', 'PERMA_SKIPPED', 'FAILED'):
        if row[0] != 'IN_PROGRESS':
            conn.close()
            return
        
    codec = get_video_codec(filepath)
    is_hevc = codec in ('hevc', 'h265')
    
    if is_hevc and old_size < (5 * 1024 * 1024 * 1024):
        if not row or row[0] != 'SKIPPED':
            c.execute('''INSERT OR REPLACE INTO conversions 
                         (filename, filepath, old_size_bytes, status, started_at, finished_at) 
                         VALUES (?, ?, ?, 'SKIPPED', ?, ?)''', 
                      (filename, filepath, old_size, datetime.now(), datetime.now()))
            conn.commit()
            print(f"[{datetime.now()}] Skipped (Already HEVC and < 5GB): {filename}")
        conn.close()
        return

    c.execute('''INSERT OR REPLACE INTO conversions 
                 (filename, filepath, old_size_bytes, status, started_at) 
                      VALUES (?, ?, ?, 'IN_PROGRESS', ?)''', 
                  (filename, filepath, old_size, datetime.now()))
    conn.commit()
    print(f"[{datetime.now()}] Transcoding: {filename}")
    
    duration = get_video_duration(filepath)
    transcode_progress = {
        "filename": filename,
        "filepath": filepath,
        "progress": 0,
        "fps": "-",
        "speed": "-",
        "eta": "-"
    }
    cancel_requested = False
    skip_current_requested = False
    tmp_filepath = filepath + ".hevc.tmp.mkv"

    cmd = [
        "docker", "run", "--rm",
        "--device=/dev/dri:/dev/dri",
        "-e", f"PUID={os.getuid()}",
        "-e", f"PGID={os.getgid()}",
        "-v", "/home/bennetgriese/plex/media:/home/bennetgriese/plex/media",
        "lscr.io/linuxserver/ffmpeg:latest",
        "-y",
        "-init_hw_device", "qsv=hw:/dev/dri/renderD128",
        "-filter_hw_device", "hw",
        "-i", filepath,
        "-vf", "format=nv12,hwupload=extra_hw_frames=64",
        "-c:v", "hevc_qsv", "-preset", "veryfast", "-async_depth", "8", "-global_quality", quality,
        "-c:a", "copy", "-c:s", "copy",
        tmp_filepath
    ]

    try:
        retryable_failure = False
        last_error_message = ""

        for attempt in range(1, MAX_TRANSCODE_RETRIES + 2):
            c.execute("UPDATE conversions SET attempt_count=?, status='IN_PROGRESS', started_at=? WHERE filepath=?", (attempt, datetime.now(), filepath))
            conn.commit()

            attempt_log = []
            try:
                current_process = subprocess.Popen(cmd, stderr=subprocess.PIPE, text=True, universal_newlines=True)
                time_regex = re.compile(r"time=(\d{2}:\d{2}:\d{2}\.\d{2})")
                speed_regex = re.compile(r"speed=\s*([\d\.]*x)")
                fps_regex = re.compile(r"fps=\s*([\d\.]+)")

                for line in current_process.stderr:
                    attempt_log.append(line)
                    if cancel_requested or skip_current_requested:
                        current_process.terminate()
                        break

                    t_match = time_regex.search(line)
                    s_match = speed_regex.search(line)
                    f_match = fps_regex.search(line)

                    if t_match:
                        time_str = t_match.group(1)
                        h, m, s = time_str.split(':')
                        parsed_sec = int(h) * 3600 + int(m) * 60 + float(s)

                        if duration > 0:
                            pct = (parsed_sec / duration) * 100
                            transcode_progress["progress"] = min(round(pct, 1), 100)

                        if s_match:
                            speed_str = s_match.group(1)
                            transcode_progress["speed"] = speed_str
                            try:
                                speed_val = float(speed_str.replace('x', ''))
                                if speed_val > 0 and duration > 0:
                                    eta_sec = (duration - parsed_sec) / speed_val
                                    transcode_progress["eta"] = f"{int(eta_sec // 60)}m {int(eta_sec % 60)}s"
                            except:
                                pass

                        if f_match:
                            transcode_progress["fps"] = f_match.group(1)

                current_process.wait()

                if skip_current_requested:
                    raise Exception("Transcoding was skipped permanently by user.")

                if cancel_requested:
                    raise Exception("Transcoding was cancelled by user.")

                if current_process.returncode == 0 and os.path.exists(tmp_filepath):
                    new_size = os.path.getsize(tmp_filepath)
                    os.remove(filepath)
                    final_filepath = os.path.splitext(filepath)[0] + ".mkv"
                    os.rename(tmp_filepath, final_filepath)

                    c.execute('''UPDATE conversions 
                                 SET status='COMPLETED', new_size_bytes=?, finished_at=?, filepath=?, error_log=NULL
                                 WHERE filepath=?''',
                              (new_size, datetime.now(), final_filepath, filepath))
                    conn.commit()
                    print(f"[{datetime.now()}] Finished: {filename}. Saved {(old_size - new_size)/1024/1024:.2f} MB")
                    break

                raise Exception("FFmpeg exited with error code " + str(current_process.returncode))

            except Exception as e:
                last_error_message = str(e)

                if os.path.exists(tmp_filepath):
                    os.remove(tmp_filepath)

                if skip_current_requested:
                    status = 'PERMA_SKIPPED'
                elif cancel_requested:
                    status = 'CANCELLED'
                else:
                    retryable_failure = is_transient_transcode_error(last_error_message + "\n" + "".join(attempt_log[-20:]))
                    if retryable_failure and attempt <= MAX_TRANSCODE_RETRIES:
                        c.execute('''UPDATE conversions 
                                     SET status='RETRYING', error_log=?, finished_at=?
                                     WHERE filepath=?''',
                                  (f"Attempt {attempt}/{MAX_TRANSCODE_RETRIES + 1}: {last_error_message}", datetime.now(), filepath))
                        conn.commit()
                        print(f"[{datetime.now()}] Retry {attempt}/{MAX_TRANSCODE_RETRIES + 1} for {filename}: {last_error_message}")
                        time.sleep(RETRY_BACKOFF_SECONDS * attempt)
                        continue

                    status = 'FAILED'

                c.execute('''UPDATE conversions 
                             SET status=?, error_log=?, finished_at=?
                             WHERE filepath=?''',
                          (status, last_error_message, datetime.now(), filepath))
                conn.commit()
                print(f"[{datetime.now()}] {status} transcoding {filename}: {last_error_message}")
                break

            finally:
                current_process = None

    finally:
        transcode_progress = {}
        skip_current_requested = False
        conn.close()

def scanner_loop():
    global is_scanning, cancel_requested, skip_current_requested
    while True:
        is_forced = force_scan_event.is_set()
        if is_night_time() or is_forced:
            is_scanning = True
            force_scan_event.clear()
            cancel_requested = False
            skip_current_requested = False
            
            settings = get_settings()
            quality = settings["quality"]
            dirs = [d["path"] for d in settings["directories"]]
            
            all_files = []
            for d in dirs:
                if os.path.exists(d):
                    all_files.extend(glob.glob(f"{d}/**/*.*", recursive=True))
            
            for f in all_files:
                if cancel_requested:
                    break
                if not is_forced and not is_night_time():
                    break
                if is_transcode_tempfile(f):
                    cleanup_transcode_tempfile(f)
                    continue
                if f.lower().endswith(ALLOWED_EXTENSIONS):
                    process_file(f, quality)
                    
            is_scanning = False
            cancel_requested = False
            skip_current_requested = False
                    
        time.sleep(10)

@app.route("/")
def index():
    return render_template('index.html')

@app.route("/api/status")
def status_api():
    cpu, temp, ram = get_sys_stats()
    drives = get_storage_stats()
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT * FROM conversions ORDER BY id DESC LIMIT 100")
    raw_rows = c.fetchall()
    conn.close()
    
    fmt_rows = []
    for r in raw_rows:
        saved = r[3] - r[4] if r[3] and r[4] else 0
        fmt_rows.append((
            r[0],
            r[1],
            r[5],
            format_size(r[3]),
            format_size(r[4]),
            format_size(saved) if saved > 0 else "-",
            r[8][:16] if r[8] else "-",
            r[6] if r[6] else ""
        ))
        
    return jsonify({
        "cpu_stats": cpu,
        "temp_stats": temp,
        "ram_stats": ram,
        "drives": drives,
        "is_scanning": is_scanning,
        "progress": transcode_progress if is_scanning else {},
        "rows": fmt_rows
    })

@app.route("/api/stats")
def stats_api():
    report = get_stats_report()

    return jsonify({
        "total_saved": report["saved"],
        "total_saved_bytes": report["saved_bytes"],
        "total_processed": report["processed"],
        "total_skipped": report["skipped"],
        "total_failed": report["failed"],
        "total_perma_skipped": report["perma_skipped"],
        "total_cancelled": report["cancelled"],
        "total_retried_jobs": report["retried_jobs"],
        "success_rate": report["success_rate"],
        "retry_rate": report["retry_rate"],
        "avg_attempts": report["avg_attempts"],
        "avg_duration_seconds": report["avg_duration_seconds"],
        "avg_duration": report["avg_duration"],
        "avg_saved": report["avg_saved"],
        "avg_saved_bytes": report["avg_saved_bytes"],
        "last_24h": {
            "completed": report["last_24h"]["completed"],
            "failed": report["last_24h"]["failed"],
            "skipped": report["last_24h"]["skipped"],
            "perma_skipped": report["last_24h"]["perma_skipped"],
            "cancelled": report["last_24h"]["cancelled"],
            "saved": report["last_24h"]["saved"],
            "saved_bytes": report["last_24h"]["saved_bytes"],
        },
        "daily": report["daily"],
        "top_savings": report["top_savings"],
        "top_errors": report["top_errors"],
    })

@app.route("/api/cancel", methods=["POST"])
def cancel_scan():
    global cancel_requested, skip_current_requested, current_process
    if is_scanning:
        cancel_requested = True
        skip_current_requested = False
        if current_process:
            current_process.terminate()
    return jsonify({"success": True})

@app.route("/api/skip_current", methods=["POST"])
def skip_current():
    global skip_current_requested, cancel_requested, current_process
    if not is_scanning or not transcode_progress.get("filepath"):
        return jsonify({"success": False, "error": "No active conversion"}), 409

    skip_current_requested = True
    cancel_requested = False
    if current_process:
        current_process.terminate()

    return jsonify({
        "success": True,
        "filepath": transcode_progress.get("filepath")
    })

@app.route("/api/homepage/status")
def homepage_status_api():
    cpu, temp, ram = get_sys_stats()

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT status, COUNT(*) FROM conversions GROUP BY status")
    counts = {row[0]: row[1] for row in c.fetchall()}

    c.execute("SELECT filename, finished_at FROM conversions WHERE status='COMPLETED' ORDER BY id DESC LIMIT 1")
    last_completed_row = c.fetchone()
    conn.close()

    progress_payload = {}
    if is_scanning and transcode_progress.get("filename"):
        progress_payload = {
            "filename": transcode_progress.get("filename"),
            "progress": transcode_progress.get("progress", 0),
            "fps": transcode_progress.get("fps", "-"),
            "speed": transcode_progress.get("speed", "-"),
            "eta": transcode_progress.get("eta", "-")
        }

    return jsonify({
        "service": "plex-transcoder",
        "online": True,
        "is_scanning": is_scanning,
        "current": progress_payload,
        "counts": {
            "completed": counts.get("COMPLETED", 0),
            "failed": counts.get("FAILED", 0),
            "skipped": counts.get("SKIPPED", 0),
            "perma_skipped": counts.get("PERMA_SKIPPED", 0),
            "in_progress": counts.get("IN_PROGRESS", 0)
        },
        "last_completed": {
            "filename": last_completed_row[0] if last_completed_row else None,
            "finished_at": last_completed_row[1] if last_completed_row else None
        },
        "host": {
            "cpu_load": cpu,
            "cpu_temp": temp,
            "ram_usage": ram
        },
        "updated_at": datetime.now().isoformat(timespec='seconds')
    })

@app.route("/api/settings")
def settings_api():
    return jsonify(get_settings())

@app.route("/api/settings/quality", methods=["POST"])
def set_quality():
    q = request.json.get("quality", "23")
    start = request.json.get("scan_start_hour", "1")
    end = request.json.get("scan_end_hour", "7")
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('quality', ?)", (q,))
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('scan_start_hour', ?)", (start,))
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('scan_end_hour', ?)", (end,))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

@app.route("/api/settings/dir", methods=["POST"])
def add_dir():
    path = request.json.get("path")
    if path:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO directories (path) VALUES (?)", (path,))
        conn.commit()
        conn.close()
    return jsonify({"success": True})

@app.route("/api/settings/dir/<int:id>", methods=["DELETE"])
def remove_dir(id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM directories WHERE id=?", (id,))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

@app.route("/api/perma_skipped")
def perma_skipped_api():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT id, filename, filepath, finished_at FROM conversions WHERE status='PERMA_SKIPPED' ORDER BY finished_at DESC, id DESC")
    items = [
        {
            "id": row[0],
            "filename": row[1],
            "filepath": row[2],
            "finished_at": row[3]
        }
        for row in c.fetchall()
    ]
    conn.close()
    return jsonify({"items": items})

@app.route("/api/perma_skipped/<int:id>", methods=["DELETE"])
def restore_perma_skipped(id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE conversions SET status='UNSKIPPED', finished_at=? WHERE id=? AND status='PERMA_SKIPPED'", (datetime.now(), id))
    conn.commit()
    updated = c.rowcount
    conn.close()
    return jsonify({"success": bool(updated), "restored": bool(updated)})

@app.route("/api/suggest_dir")
def suggest_dir():
    path_input = request.args.get("path", "")
    if not path_input:
        path_input = "/"

    if path_input.endswith('/'):
        base_dir = path_input
        prefix = ""
    else:
        base_dir = os.path.dirname(path_input)
        prefix = os.path.basename(path_input)

    if not os.path.exists(base_dir) or not os.path.isdir(base_dir):
        return jsonify({"folders": []})

    try:
        items = os.listdir(base_dir)
    except Exception:
        return jsonify({"folders": []})

    folders = []
    for item in sorted(items):
        item_path = os.path.join(base_dir, item)
        if os.path.isdir(item_path) and item.lower().startswith(prefix.lower()):
            folders.append({"name": item, "path": item_path})

    return jsonify({"folders": folders})

@app.route("/start_scan", methods=["POST"])
def manual_start():
    force_scan_event.set()
    time.sleep(1) 
    return redirect("/")

@app.route("/api/retry_all_failed", methods=["POST"])
def retry_all_failed():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM conversions WHERE status='FAILED'")
    conn.commit()
    conn.close()
    return jsonify({"success": True})

if __name__ == '__main__':
    init_db()
    
    t = threading.Thread(target=scanner_loop, daemon=True)
    t.start()
    
    app.run(host='0.0.0.0', port=5050)
