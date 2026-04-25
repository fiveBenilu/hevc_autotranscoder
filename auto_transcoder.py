import os
import glob
import subprocess
import sqlite3
import time
import threading
import json
import re
from datetime import datetime
from flask import Flask, render_template_string, jsonify, request, redirect

app = Flask(__name__)

DB_FILE = "transcoder.db"
ALLOWED_EXTENSIONS = ('.mkv', '.mp4', '.avi', '.mov')

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
            finished_at DATETIME
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
    c.execute("SELECT value FROM settings WHERE key='quality'")
    row = c.fetchone()
    q = row[0] if row else "23"
    
    c.execute("SELECT id, path FROM directories")
    dirs = [{"id": r[0], "path": r[1]} for r in c.fetchall()]
    conn.close()
    return {"quality": q, "directories": dirs}

def is_night_time():
    hour = datetime.now().hour
    return 1 <= hour < 7

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

def process_file(filepath, quality):
    global current_process, transcode_progress, cancel_requested, skip_current_requested
    
    filename = os.path.basename(filepath)
    try:
        old_size = os.path.getsize(filepath)
    except:
        return
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    c.execute("SELECT status FROM conversions WHERE filepath=?", (filepath,))
    row = c.fetchone()
    
    if row and row[0] in ('COMPLETED', 'IN_PROGRESS', 'SKIPPED', 'PERMA_SKIPPED'):
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
        "-v", "/home/bennetgriese/plex/media:/home/bennetgriese/plex/media",
        "lscr.io/linuxserver/ffmpeg:latest",
        "-y", 
        "-vaapi_device", "/dev/dri/renderD128",
        "-i", filepath,
        "-vf", "format=nv12,hwupload",
        "-c:v", "hevc_vaapi", "-global_quality", quality,
        "-c:a", "copy", "-c:s", "copy",
        tmp_filepath
    ]
    
    try:
        current_process = subprocess.Popen(cmd, stderr=subprocess.PIPE, text=True, universal_newlines=True)
        time_regex = re.compile(r"time=(\d{2}:\d{2}:\d{2}\.\d{2})")
        speed_regex = re.compile(r"speed=\s*([\d\.]*x)")
        fps_regex = re.compile(r"fps=\s*([\d\.]+)")
        
        for line in current_process.stderr:
            if cancel_requested or skip_current_requested:
                current_process.terminate()
                break
                
            t_match = time_regex.search(line)
            s_match = speed_regex.search(line)
            f_match = fps_regex.search(line)
            
            if t_match:
                time_str = t_match.group(1)
                h, m, s = time_str.split(':')
                parsed_sec = int(h)*3600 + int(m)*60 + float(s)
                
                if duration > 0:
                    pct = (parsed_sec / duration) * 100
                    transcode_progress["progress"] = min(round(pct, 1), 100)
                    
                if s_match:
                    speed_str = s_match.group(1)
                    transcode_progress["speed"] = speed_str
                    try:
                        speed_val = float(speed_str.replace('x',''))
                        if speed_val > 0 and duration > 0:
                            eta_sec = (duration - parsed_sec) / speed_val
                            transcode_progress["eta"] = f"{int(eta_sec//60)}m {int(eta_sec%60)}s"
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
                         SET status='COMPLETED', new_size_bytes=?, finished_at=?, filepath=?
                         WHERE filepath=?''', 
                      (new_size, datetime.now(), final_filepath, filepath))
            conn.commit()
            print(f"[{datetime.now()}] Finished: {filename}. Saved {(old_size - new_size)/1024/1024:.2f} MB")
        else:
            raise Exception("FFmpeg exited with error code " + str(current_process.returncode))
            
    except Exception as e:
        if os.path.exists(tmp_filepath):
            os.remove(tmp_filepath)
        if skip_current_requested:
            status = 'PERMA_SKIPPED'
        elif cancel_requested:
            status = 'CANCELLED'
        else:
            status = 'FAILED'
        c.execute('''UPDATE conversions 
                     SET status=?, error_log=?, finished_at=?
                     WHERE filepath=?''', 
                  (status, str(e), datetime.now(), filepath))
        conn.commit()
        print(f"[{datetime.now()}] {status} transcoding {filename}: {e}")
        
    finally:
        current_process = None
        transcode_progress = {}
        skip_current_requested = False
        conn.close()

def scanner_loop():
    global is_scanning, cancel_requested, skip_current_requested
    while True:
        if is_night_time() or force_scan_event.is_set():
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
                if f.lower().endswith(ALLOWED_EXTENSIONS):
                    process_file(f, quality)
                    
            is_scanning = False
            cancel_requested = False
            skip_current_requested = False
                    
        time.sleep(10)

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Plex Transcoder</title>
    <style>
        :root {
            --bg-color: #f5f5f7;
            --card-bg: #ffffff;
            --text-main: #1d1d1f;
            --text-sec: #86868b;
            --border: rgba(60,60,67,0.1);
            --acc-blue: #007aff;
            --acc-green: #34c759;
            --acc-red: #ff3b30;
            --acc-yellow: #ffcc00;
            --control-bg: #e3e3e8;
            --control-active: #ffffff;
            --shadow-sm: 0 4px 14px rgba(0,0,0,0.03);
            --shadow-md: 0 1px 3px rgba(0,0,0,0.1);
        }
        @media (prefers-color-scheme: dark) {
            :root {
                --bg-color: #000000;
                --card-bg: #1c1c1e;
                --text-main: #f2f2f7;
                --text-sec: #aeaeb2;
                --border: rgba(84,84,88,0.65);
                --acc-blue: #0a84ff;
                --acc-green: #32d74b;
                --acc-red: #ff453a;
                --acc-yellow: #ffd60a;
                --control-bg: #2c2c2e;
                --control-active: #636366;
                --shadow-sm: 0 4px 14px rgba(0,0,0,0.4);
                --shadow-md: 0 1px 3px rgba(0,0,0,0.3);
            }
        }
        * { box-sizing: border-box; -webkit-font-smoothing: antialiased; }
        body { 
            font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", Roboto, Helvetica, Arial, sans-serif; 
            background-color: var(--bg-color); 
            color: var(--text-main); 
            padding: 0; 
            margin: 0; 
        }
        .navbar {
            background-color: rgba(255, 255, 255, 0.7);
            backdrop-filter: blur(20px);
            -webkit-backdrop-filter: blur(20px);
            border-bottom: 1px solid var(--border);
            padding: 12px 20px;
            position: sticky;
            top: 0;
            z-index: 50;
            display: flex;
            justify-content: center;
        }
        @media (prefers-color-scheme: dark) {
            .navbar { background-color: rgba(28, 28, 30, 0.7); }
        }
        .nav-content { max-width: 1100px; width: 100%; display: flex; justify-content: space-between; align-items: center; }
        .nav-title { font-weight: 600; font-size: 18px; display: flex; align-items: center; gap: 8px; letter-spacing: -0.3px;}
        
        .container { max-width: 1100px; width: 100%; margin: 40px auto; padding: 0 20px; }
        
        h1 { font-weight: 700; font-size: 34px; margin-bottom: 8px; letter-spacing: -0.5px; }
        p.subtitle { color: var(--text-sec); margin-top: 0; margin-bottom: 30px; font-size: 16px; font-weight: 400; }
        
        /* Apple Segmented Control */
        .segmented-control { display: inline-flex; background: var(--control-bg); border-radius: 9px; padding: 2px; margin-bottom: 30px; }
        .tab { cursor: pointer; font-size: 13px; font-weight: 500; color: var(--text-main); padding: 6px 18px; border-radius: 7px; transition: all 0.2s ease; }
        .tab.active { background: var(--control-active); box-shadow: var(--shadow-md); }
        .tab-content { display: none; animation: fadeIn 0.3s ease; }
        .tab-content.active { display: block; }
        @keyframes fadeIn { from { opacity: 0; transform: translateY(5px); } to { opacity: 1; transform: translateY(0); } }

        .header-cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 20px; margin-bottom: 30px; }
        .card { 
            background-color: var(--card-bg); 
            border-radius: 18px; 
            padding: 24px; 
            box-shadow: var(--shadow-sm);
            display: flex;
            flex-direction: column;
            gap: 12px;
            position: relative;
            overflow: hidden;
        }
        .card-header { display: flex; align-items: center; gap: 8px; color: var(--text-sec); font-size: 13px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px;}
        .card-value { font-size: 28px; font-weight: 700; letter-spacing: -0.5px; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
        
        .btn { 
            background-color: var(--acc-blue); color: white; padding: 12px 20px; border-radius: 20px; border: none; 
            cursor: pointer; font-size: 15px; font-weight: 600; display: inline-flex; align-items: center; gap: 8px;
            transition: all 0.2s ease; text-decoration: none; justify-content: center;
        }
        .btn-red { background-color: var(--acc-red); color: white; }
        .btn:hover { opacity: 0.9; transform: scale(0.98); }
        
        .sp { animation: spin 2s linear infinite; }
        @keyframes spin { 100% { transform: rotate(360deg); } }
        
        /* List Style UI */
        .list-card { background: var(--card-bg); border-radius: 18px; box-shadow: var(--shadow-sm); overflow: hidden; margin-bottom: 30px; padding: 0;}
        .list-header { padding: 16px 20px; border-bottom: 1px solid var(--border); background: rgba(0,0,0,0.01); display: flex; justify-content: space-between; align-items: center; }
        .list-title { font-size: 17px; font-weight: 600; letter-spacing: -0.3px; }
        
        .table-responsive { width: 100%; overflow-x: auto; -webkit-overflow-scrolling: touch; }
        table { width: 100%; border-collapse: collapse; background-color: var(--card-bg); border-radius: 14px; overflow: hidden; border: 1px solid var(--border); box-shadow: 0 4px 6px rgba(0,0,0,0.02); }
        th, td { padding: 14px 16px; text-align: left; font-size: 14px; border-bottom: 1px solid var(--border); }
        th { color: var(--text-sec); font-weight: 500; font-size: 13px; text-transform: uppercase; letter-spacing: 0.5px; }
        tr:last-child td { border-bottom: none; }
        
        .status-badge { display: inline-flex; align-items: center; padding: 4px 10px; border-radius: 12px; font-size: 12px; font-weight: 600; }
        .COMPLETED { background-color: rgba(52, 199, 89, 0.15); color: var(--acc-green); }
        .FAILED { background-color: rgba(255, 59, 48, 0.15); color: var(--acc-red); }
        .CANCELLED { background-color: rgba(255, 59, 48, 0.15); color: var(--acc-red); }
        .IN_PROGRESS { background-color: rgba(255, 204, 0, 0.15); color: var(--acc-yellow); }
        .SKIPPED { background-color: rgba(142, 142, 147, 0.15); color: var(--text-sec); }
        .PERMA_SKIPPED { background-color: rgba(142, 142, 147, 0.15); color: var(--text-sec); }
        
        .icon { width: 18px; height: 18px; display: block; }
        .icon-sm { width: 16px; height: 16px; display: block; min-width: 16px;}
        .icon-lg { width: 28px; height: 28px; display: block; margin-right: 6px; color: var(--acc-blue); }
        
        .form-group { margin-bottom: 20px; padding: 0 20px;}
        label { display: block; font-weight: 500; margin-bottom: 12px; font-size: 14px;}
        input[type="range"] { width: 100%; max-width: 400px; accent-color: var(--acc-blue); }
        .range-labels { display: flex; justify-content: space-between; max-width: 400px; color: var(--text-sec); font-size: 12px; margin-top: 8px; font-weight: 500; }
        input[type="text"] { width: 100%; max-width: 400px; padding: 12px 16px; border-radius: 10px; border: 1px solid var(--border); background: transparent; color: var(--text-main); font-size: 15px;}
        input[type="text"]:focus { outline: none; border-color: var(--acc-blue); box-shadow: 0 0 0 2px rgba(0, 122, 255, 0.2); }
        
        .dir-list { list-style: none; padding: 0; margin: 0; }
        .dir-item { display: flex; justify-content: space-between; align-items: center; padding: 14px 20px; border-bottom: 1px solid var(--border); }
        .dir-item:last-child { border-bottom: none; }
        .dir-path { font-size: 15px; font-weight: 500; }
        
        /* Grid Layout for specific layout scenarios */
        .layout-grid { display: grid; grid-template-columns: 2fr 1fr; gap: 24px; align-items: stretch;}
        @media (max-width: 900px) {
            .layout-grid { grid-template-columns: 1fr; gap: 16px; }
            .container { margin: 20px auto; padding: 0 16px; }
            h1 { font-size: 28px; }
            .btn { width: 100%; justify-content: center; }
            #action-card { padding: 16px 0 !important; align-items: stretch !important; flex: none !important; }
            .card { padding: 20px; }
            .card-value { font-size: 24px !important; }
            
            /* Responsive Table -> Cards for Mobile */
            .table-responsive { background: transparent; border: none; box-shadow: none; overflow: visible; }
            table { border: none; background: transparent; box-shadow: none; display: block; border-radius: 0; }
            thead { display: none; }
            tbody { display: flex; flex-direction: column; gap: 12px; }
            tr.table-row-card { display: flex; flex-direction: column; background: var(--card-bg); border-radius: 16px; padding: 16px; border: 1px solid var(--border); box-shadow: var(--shadow-sm); position: relative; }
            tr.table-row-card td { display: flex; justify-content: space-between; align-items: center; border: none; padding: 6px 0; font-size: 14px; white-space: normal; line-height: 1.4; }
            tr.table-row-card td::before { content: attr(data-label); font-size: 12px; font-weight: 500; color: var(--text-sec); text-transform: uppercase; letter-spacing: 0.5px; width: 40%; flex-shrink: 0; }
            tr.table-row-card td.td-filename { font-weight: 600 !important; font-size: 15px; margin-bottom: 8px; flex-direction: column; align-items: flex-start; padding-bottom: 8px; padding-right: 105px; border-bottom: 1px solid var(--border); word-break: break-all; }
            tr.table-row-card td.td-filename::before { display: none; }
            tr.table-row-card td.td-status { position: absolute; top: 12px; right: 16px; padding: 0; }
            tr.table-row-card td.td-status::before { display: none; }
        }
    </style>
    <script>
        function switchTab(event, tabId) {
            if (event) {
                document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
                event.target.classList.add('active');
            }
            document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
            document.getElementById(tabId).classList.add('active');
            if(tabId === 'stats') loadStats();
            if(tabId === 'settings') loadSettings();
        }

        function cancelScan() {
            if(confirm("Cancel the current transcoding job? The original file will be safely kept and the temporary file deleted.")) {
                fetch('/api/cancel', { method: 'POST' }).then(() => updateDashboard());
            }
        }

        function skipCurrentMedia() {
            if(confirm("Skip this media permanently? It will be written to DB and never transcoded again.")) {
                fetch('/api/skip_current', { method: 'POST' }).then(() => updateDashboard());
            }
        }

        function updateDashboard() {
            fetch('/api/status')
            .then(response => response.json())
            .then(data => {
                document.getElementById('cpu-stats').innerText = data.cpu_stats;
                document.getElementById('temp-stats').innerText = data.temp_stats;
                document.getElementById('ram-stats').innerText = data.ram_stats;
                
                const statusCard = document.getElementById('status-card');
                const actionCard = document.getElementById('action-card');
                
                if (data.is_scanning) {
                    let progHtml = '';
                    if(data.progress && data.progress.filename) {
                        progHtml = `
                        <div style="width: 100%; margin-top: 15px; font-size: 14px;">
                            <div style="display: flex; justify-content: space-between; margin-bottom: 8px;">
                                <span style="white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 70%; font-weight: 500;">${data.progress.filename}</span>
                                <span style="font-weight: 700; color: var(--acc-blue);">${data.progress.progress}%</span>
                            </div>
                            <div style="width: 100%; height: 8px; background: var(--control-bg); border-radius: 4px; overflow: hidden;">
                                <div style="width: ${data.progress.progress}%; height: 100%; background: var(--acc-blue); transition: width 0.4s ease-out;"></div>
                            </div>
                            <div style="display: flex; justify-content: space-between; margin-top: 8px; color: var(--text-sec); font-size: 13px; font-weight: 500;">
                                <span>Speed: ${data.progress.speed} &bull; FPS: ${data.progress.fps}</span>
                                <span>ETA: ${data.progress.eta}</span>
                            </div>
                        </div>`;
                    }

                    statusCard.innerHTML = `<span style="display:flex; align-items:center; gap:8px;"><svg class="icon-sm sp" style="color: var(--acc-blue);" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="2" x2="12" y2="6"></line><line x1="12" y1="18" x2="12" y2="22"></line><line x1="4.93" y1="4.93" x2="7.76" y2="7.76"></line><line x1="16.24" y1="16.24" x2="19.07" y2="19.07"></line><line x1="2" y1="12" x2="6" y2="12"></line><line x1="18" y1="12" x2="22" y2="12"></line><line x1="4.93" y1="19.07" x2="7.76" y2="16.24"></line><line x1="16.24" y1="4.93" x2="19.07" y2="7.76"></line></svg> Transcoding...</span>${progHtml}`;
                    
                    actionCard.innerHTML = `<div style="display:flex; flex-direction:column; gap:12px; width:100%;">
                        <button class="btn" onclick="skipCurrentMedia()" style="width: 100%; background: var(--acc-yellow); color: #1d1d1f;">
                        <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14"></path><path d="M12 5v14"></path></svg>
                        Skip Forever</button>
                        <button class="btn btn-red" onclick="cancelScan()" style="width: 100%;">
                        <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>
                        Cancel Job</button>
                    </div>`;
                } else {
                    statusCard.innerHTML = "Idle";
                    actionCard.innerHTML = `<form action="/start_scan" method="POST" style="margin:0; width: 100%;">
                        <button class="btn" id="start-btn" type="submit" style="width: 100%;">
                        <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polygon points="5 3 19 12 5 21 5 3"></polygon></svg>
                        Start Scan</button></form>`;
                }

                let rowsHtml = '';
                data.rows.forEach(row => {
                    let fileName = row[1];
                    rowsHtml += `<tr class="table-row-card">
                        <td data-label="Filename" class="td-filename" style="font-weight: 500;">${fileName}</td>
                        <td data-label="Status" class="td-status"><span class="status-badge ${row[2]}">${row[2]}</span></td>
                        <td data-label="Orig. Size" style="color: var(--text-sec);">${row[3]}</td>
                        <td data-label="New Size" style="color: var(--text-sec);">${row[4]}</td>
                        <td data-label="Saved" style="font-weight: 600; color: var(--acc-green);">${row[5]}</td>
                        <td data-label="Finished" style="color: var(--text-sec);">${row[6]}</td>
                    </tr>`;
                });
                document.getElementById('table-body').innerHTML = rowsHtml;

                let drivesHtml = '<div class="header-cards" style="margin-bottom: 24px;">';
                data.drives.forEach(drive => {
                    drivesHtml += `<div class="card">
                        <div class="card-header">
                            <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="2" width="20" height="8" rx="2" ry="2"></rect><rect x="2" y="14" width="20" height="8" rx="2" ry="2"></rect><line x1="6" y1="6" x2="6" y2="6"></line><line x1="6" y1="18" x2="6" y2="18"></line></svg>
                            ${drive.mount}
                        </div>
                        <div class="card-value" style="font-size: 24px;">${drive.free} free</div>
                        <div style="color: var(--text-sec); font-size: 13px; font-weight: 500; margin-top: 4px;">Total: ${drive.total} &bull; ${drive.perc} used</div>
                    </div>`;
                });
                drivesHtml += '</div>';
                document.getElementById('drives-container').innerHTML = drivesHtml;
            });
        }
        
        function loadStats() {
            fetch('/api/stats')
            .then(res => res.json())
            .then(data => {
                document.getElementById('total-saved').innerText = data.total_saved;
                document.getElementById('total-processed').innerText = data.total_processed;
                document.getElementById('total-skipped').innerText = data.total_skipped;
                document.getElementById('total-failed').innerText = data.total_failed;
                document.getElementById('total-perma-skipped').innerText = data.total_perma_skipped;
            });
        }
        
        function loadSettings() {
            fetch('/api/settings')
            .then(res => res.json())
            .then(data => {
                const qSlider = document.getElementById('quality-slider');
                qSlider.value = data.quality === '18' ? 3 : (data.quality === '23' ? 2 : 1);
                
                let dirHtml = '';
                data.directories.forEach(d => {
                    dirHtml += `<li class="dir-item">
                        <span class="dir-path">${d.path}</span>
                        <button onclick="removeDir(${d.id})" class="btn btn-red" style="padding: 6px 14px; font-size: 13px; background: rgba(255, 59, 48, 0.15); color: var(--acc-red);">Remove</button>
                    </li>`;
                });
                document.getElementById('dir-list').innerHTML = dirHtml;
            });
        }
        
        function saveQuality() {
            const val = document.getElementById('quality-slider').value;
            const q = val == 3 ? '18' : (val == 2 ? '23' : '28');
            fetch('/api/settings/quality', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({quality: q})
            });
        }
        
        function suggestDir() {
            const input = document.getElementById('new-dir');
            const drop = document.getElementById('dir-suggestions');
            let val = input.value;
            
            if (val.length === 0) val = '/';
            
            fetch('/api/suggest_dir?path=' + encodeURIComponent(val))
            .then(r => r.json())
            .then(data => {
                if (data.folders && data.folders.length > 0) {
                    drop.innerHTML = data.folders.map(f => {
                        const safePath = f.path.replace(/"/g, '&quot;');
                        return `<div style="padding: 12px 16px; cursor: pointer; border-bottom: 1px solid var(--border); display: flex; align-items: center; font-size: 14px;" 
                                      onclick="selectDir(event, this.dataset.path)" data-path="${safePath}"
                                      onmouseover="this.style.background='var(--bg-color)'"
                                      onmouseout="this.style.background='transparent'">
                                    <svg class="icon-sm" viewBox="0 0 24 24" fill="none" stroke="var(--acc-blue)" stroke-width="2" style="margin-right: 12px;"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path></svg>
                                    ${f.name}
                                 </div>`;
                    }).join('');
                    drop.style.display = 'block';
                } else {
                    drop.style.display = 'none';
                }
            });
        }
        
        function selectDir(e, path) {
            e.preventDefault();
            e.stopPropagation();
            const input = document.getElementById('new-dir');
            input.value = path + '/';
            document.getElementById('dir-suggestions').style.display = 'none';
            input.focus();
            suggestDir(); 
        }

        document.addEventListener('click', function(e) {
            if (e.target.id !== 'new-dir') {
                const drop = document.getElementById('dir-suggestions');
                if(drop) drop.style.display = 'none';
            }
        });

        function addDir(event) {
            event.preventDefault();
            const input = document.getElementById('new-dir');
            fetch('/api/settings/dir', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({path: input.value})
            }).then(() => { 
                input.value = ''; 
                document.getElementById('dir-suggestions').style.display = 'none';
                loadSettings(); 
            });
        }
        
        function removeDir(id) {
            fetch('/api/settings/dir/' + id, {method: 'DELETE'})
            .then(() => loadSettings());
        }
        
        setInterval(() => {
            if(document.getElementById('dashboard').classList.contains('active')) updateDashboard();
        }, 1500);
        
        window.onload = function() {
            updateDashboard();
        }
    </script>
</head>
<body>
    <div class="navbar">
        <div class="nav-content">
            <span class="nav-title">
                <svg class="icon-lg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polygon points="23 7 16 12 23 17 23 7"></polygon><rect x="1" y="5" width="15" height="14" rx="2" ry="2"></rect></svg>
                Transcoder
            </span>
            <div class="segmented-control" style="margin-bottom:0;">
                <div class="tab active" onclick="switchTab(event, 'dashboard')">Dashboard</div>
                <div class="tab" onclick="switchTab(event, 'stats')">Statistics</div>
                <div class="tab" onclick="switchTab(event, 'settings')">Settings</div>
            </div>
        </div>
    </div>

    <div class="container">
        <h1>Overview</h1>
        <p class="subtitle">Hardware accelerated with Intel QSV. Automatically runs between 01:00 and 07:00.</p>

        <div id="dashboard" class="tab-content active">
            <div class="layout-grid">
                <div>
                    <div class="header-cards">
                        <div class="card">
                            <div class="card-header">
                                <svg class="icon-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><rect x="4" y="4" width="16" height="16" rx="2" ry="2"></rect><rect x="9" y="9" width="6" height="6"></rect></svg>
                                CPU Load
                            </div>
                            <div id="cpu-stats" class="card-value">-</div>
                        </div>

                        <div class="card">
                            <div class="card-header">
                                <svg class="icon-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M14 14.76V3.5a2.5 2.5 0 0 0-5 0v11.26a4.5 4.5 0 1 0 5 0z"></path></svg>
                                CPU Temp
                            </div>
                            <div id="temp-stats" class="card-value">-</div>
                        </div>
                        
                        <div class="card" style="grid-column: 1 / -1;">
                            <div class="card-header">
                                <svg class="icon-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="4" y1="8" x2="20" y2="8"></line><line x1="4" y1="16" x2="20" y2="16"></line><line x1="8" y1="4" x2="8" y2="20"></line><line x1="16" y1="4" x2="16" y2="20"></line></svg>
                                Memory Usage
                            </div>
                            <div id="ram-stats" class="card-value" style="font-size: 20px;">-</div>
                        </div>
                        
                        <div class="card" style="grid-column: 1 / -1;">
                            <div class="card-header">
                                <svg class="icon-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="10"></circle><polyline points="12 6 12 12 16 14"></polyline></svg>
                                Current Job Status
                            </div>
                            <div id="status-card" class="card-value" style="font-size: 22px; align-items: flex-start; flex-direction: column; width: 100%;">
                                Idle
                            </div>
                        </div>
                    </div>
                </div>
                <div id="action-card" class="card" style="border:none; box-shadow:none; background:transparent; padding:0; flex: 0.5; justify-content:center; align-items:flex-end;">
                </div>
            </div>
            
            <div id="drives-container"></div>

            <div class="table-responsive">
                <table>
                    <thead>
                        <tr>
                            <th>Filename</th>
                            <th>Status</th>
                            <th>Orig. Size</th>
                            <th>New Size</th>
                            <th>Saved</th>
                            <th>Finished</th>
                        </tr>
                    </thead>
                    <tbody id="table-body">
                    </tbody>
                </table>
            </div>
        </div>

        <div id="stats" class="tab-content">
            <div class="header-cards">
                <div class="card">
                    <div class="card-header">Total Space Saved</div>
                    <div id="total-saved" class="card-value" style="color: var(--acc-green); font-size: 32px;">-</div>
                </div>
                <div class="card">
                    <div class="card-header">Files Processed</div>
                    <div id="total-processed" class="card-value" style="font-size: 32px;">-</div>
                </div>
                <div class="card">
                    <div class="card-header">Files Skipped</div>
                    <div id="total-skipped" class="card-value" style="font-size: 32px;">-</div>
                </div>
                <div class="card">
                    <div class="card-header">Failed Conversions</div>
                    <div id="total-failed" class="card-value" style="color: var(--acc-red); font-size: 32px;">-</div>
                </div>
                <div class="card">
                    <div class="card-header">Permanently Skipped</div>
                    <div id="total-perma-skipped" class="card-value" style="font-size: 32px;">-</div>
                </div>
            </div>
        </div>

        <div id="settings" class="tab-content">
            <div class="list-card">
                <div class="list-header">
                    <span class="list-title">Transcoding Quality</span>
                </div>
                <div style="padding: 20px 0;">
                    <div class="form-group">
                        <label>Quality Setting (FFmpeg -global_quality)</label>
                        <input type="range" id="quality-slider" min="1" max="3" step="1" onchange="saveQuality()">
                        <div class="range-labels">
                            <span>Low/28 (Smaller Size)</span>
                            <span>Medium/23 (Balanced)</span>
                            <span>High/18 (Better Video)</span>
                        </div>
                    </div>
                </div>
            </div>
            
            <div class="list-card">
                <div class="list-header">
                    <span class="list-title">Monitored Directories</span>
                </div>
                <ul id="dir-list" class="dir-list"></ul>
                <div style="padding: 20px; border-top: 1px solid var(--border); background: rgba(0,0,0,0.01);">
                    <form onsubmit="addDir(event)" style="margin: 0;">
                        <label>Add new directory</label>
                        <div style="display: flex; gap: 12px; position: relative;">
                            <div style="flex: 1; position: relative;">
                                <input type="text" id="new-dir" style="width: 100%; box-sizing: border-box;" placeholder="Type /home/... to browse" oninput="suggestDir()" onclick="suggestDir()" autocomplete="off" required>
                                <div id="dir-suggestions" style="display: none; position: absolute; width: 100%; top: calc(100% + 8px); background: var(--card-bg); border: 1px solid var(--border); border-radius: 12px; max-height: 250px; overflow-y: auto; box-shadow: 0 10px 30px rgba(0,0,0,0.15); z-index: 100;"></div>
                            </div>
                            <button type="submit" class="btn">Add Directory</button>
                        </div>
                    </form>
                </div>
            </div>
        </div>
    </div>
</body>
</html>
"""

@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)

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
            r[8][:16] if r[8] else "-"
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
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT SUM(old_size_bytes - new_size_bytes) FROM conversions WHERE status='COMPLETED'")
    saved = c.fetchone()[0] or 0
    
    c.execute("SELECT COUNT(*) FROM conversions WHERE status='COMPLETED'")
    processed = c.fetchone()[0]
    
    c.execute("SELECT COUNT(*) FROM conversions WHERE status='SKIPPED'")
    skipped = c.fetchone()[0]
    
    c.execute("SELECT COUNT(*) FROM conversions WHERE status='FAILED'")
    failed = c.fetchone()[0]

    c.execute("SELECT COUNT(*) FROM conversions WHERE status='PERMA_SKIPPED'")
    perma_skipped = c.fetchone()[0]
    
    conn.close()
    
    return jsonify({
        "total_saved": format_size(saved),
        "total_processed": processed,
        "total_skipped": skipped,
        "total_failed": failed,
        "total_perma_skipped": perma_skipped
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
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('quality', ?)", (q,))
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

if __name__ == '__main__':
    init_db()
    
    t = threading.Thread(target=scanner_loop, daemon=True)
    t.start()
    
    app.run(host='0.0.0.0', port=5050)
