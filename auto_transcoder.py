import os
import glob
import subprocess
import sqlite3
import time
import threading
from datetime import datetime
from flask import Flask, render_template_string, jsonify

app = Flask(__name__)

DB_FILE = "transcoder.db"
MOVIES_DIR = "/home/bennetgriese/plex/media/movies"
TV_DIR = "/home/bennetgriese/plex/media/tv"

ALLOWED_EXTENSIONS = ('.mkv', '.mp4', '.avi', '.mov')

# Events & state
force_scan_event = threading.Event()
is_scanning = False

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
        cpu_str = f"Load (1m): {round(load1,2)}"
    except:
        cpu_str = "N/A"
        
    return cpu_str, ram_str

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
    conn.commit()
    conn.close()

def is_night_time():
    hour = datetime.now().hour
    # Run only between 01:00 AM and 07:00 AM
    return 1 <= hour < 7

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

def process_file(filepath):
    filename = os.path.basename(filepath)
    old_size = os.path.getsize(filepath)
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    # Check if already in DB
    c.execute("SELECT status FROM conversions WHERE filepath=?", (filepath,))
    row = c.fetchone()
    
    # If completed or in progress, ignore
    if row and row[0] in ('COMPLETED', 'IN_PROGRESS'):
        conn.close()
        return
        
    codec = get_video_codec(filepath)
    is_hevc = codec in ('hevc', 'h265')
    
    # Skip if it is already HEVC AND smaller than 5 GB
    # If it is HEVC but >= 5 GB, we will transcode it again to shrink it.
    # If it was previously marked as SKIPPED, we check again.
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

    # Start Conversion
    c.execute('''INSERT OR REPLACE INTO conversions 
                 (filename, filepath, old_size_bytes, status, started_at) 
                 VALUES (?, ?, ?, 'IN_PROGRESS', ?)''', 
              (filename, filepath, old_size, datetime.now()))
    conn.commit()
    print(f"[{datetime.now()}] Transcoding: {filename}")
    
    # Needs HEVC
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
        "-c:v", "hevc_vaapi", "-global_quality", "23",
        "-c:a", "copy", "-c:s", "copy",
        tmp_filepath
    ]
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0 and os.path.exists(tmp_filepath):
            new_size = os.path.getsize(tmp_filepath)
            
            # Replace original
            os.remove(filepath)
            final_filepath = os.path.splitext(filepath)[0] + ".mkv" # Force MKV container if it was mp4/mov
            os.rename(tmp_filepath, final_filepath)
            
            c.execute('''UPDATE conversions 
                         SET status='COMPLETED', new_size_bytes=?, finished_at=?, filepath=?
                         WHERE filepath=?''', 
                      (new_size, datetime.now(), final_filepath, filepath))
            conn.commit()
            print(f"[{datetime.now()}] Finished: {filename}. Saved {(old_size - new_size)/1024/1024:.2f} MB")
        else:
            raise Exception(result.stderr[-500:]) # Last 500 chars of error
            
    except Exception as e:
        if os.path.exists(tmp_filepath):
            os.remove(tmp_filepath)
        c.execute('''UPDATE conversions 
                     SET status='FAILED', error_log=?, finished_at=?
                     WHERE filepath=?''', 
                  (str(e), datetime.now(), filepath))
        conn.commit()
        print(f"[{datetime.now()}] Error transcodoing {filename}: {e}")
        
    conn.close()

def scanner_loop():
    global is_scanning
    while True:
        if is_night_time() or force_scan_event.is_set():
            is_scanning = True
            force_scan_event.clear()
            
            movies = glob.glob(f"{MOVIES_DIR}/**/*.*", recursive=True)
            tv = glob.glob(f"{TV_DIR}/**/*.*", recursive=True)
            all_files = movies + tv
            
            for f in all_files:
                if f.lower().endswith(ALLOWED_EXTENSIONS):
                    process_file(f)
                    
            is_scanning = False
                    
        # Sleep incrementally up to 5 minutes, checking the event
        force_scan_event.wait(300)

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
            --border: #e5e5ea;
            --acc-blue: #007aff;
            --acc-green: #34c759;
            --acc-red: #ff3b30;
            --acc-yellow: #ffcc00;
        }
        @media (prefers-color-scheme: dark) {
            :root {
                --bg-color: #000000;
                --card-bg: #1c1c1e;
                --text-main: #f2f2f7;
                --text-sec: #aeaeb2;
                --border: #38383a;
                --acc-blue: #0a84ff;
                --acc-green: #32d74b;
                --acc-red: #ff453a;
                --acc-yellow: #ffd60a;
            }
        }
        * { box-sizing: border-box; }
        body { 
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; 
            background-color: var(--bg-color); 
            color: var(--text-main); 
            padding: 40px 20px; 
            margin: 0; 
            display: flex;
            justify-content: center;
        }
        .container { max-width: 1000px; width: 100%; }
        h1 { font-weight: 600; font-size: 28px; margin-bottom: 5px; display: flex; align-items: center; gap: 10px; }
        p.subtitle { color: var(--text-sec); margin-top: 0; margin-bottom: 30px; font-size: 15px; }
        
        .header-cards { display: flex; gap: 20px; margin-bottom: 30px; flex-wrap: wrap; }
        .card { 
            background-color: var(--card-bg); 
            border: 1px solid var(--border); 
            border-radius: 14px; 
            padding: 20px; 
            flex: 1; 
            min-width: 180px;
            box-shadow: 0 4px 6px rgba(0,0,0,0.02);
            display: flex;
            flex-direction: column;
            gap: 8px;
        }
        .card-header { display: flex; align-items: center; gap: 8px; color: var(--text-sec); font-size: 14px; font-weight: 500;}
        .card-value { font-size: 20px; font-weight: 600; display: flex; align-items: center; gap: 8px; }
        
        .btn { 
            background-color: var(--acc-blue); 
            color: white; 
            padding: 12px 24px; 
            border-radius: 20px; 
            border: none; 
            cursor: pointer; 
            font-size: 15px; 
            font-weight: 600;
            display: inline-flex; 
            align-items: center; 
            gap: 8px;
            transition: all 0.2s ease;
        }
        .btn:hover { opacity: 0.9; transform: scale(0.98); }
        .btn-disabled { background-color: var(--border); color: var(--text-sec); cursor: not-allowed; }
        .btn-disabled:hover { opacity: 1; transform: scale(1); }
        
        .sp { animation: spin 2s linear infinite; }
        @keyframes spin { 100% { transform: rotate(360deg); } }
        
        table { width: 100%; border-collapse: collapse; background-color: var(--card-bg); border-radius: 14px; overflow: hidden; border: 1px solid var(--border); box-shadow: 0 4px 6px rgba(0,0,0,0.02); }
        th, td { padding: 14px 16px; text-align: left; font-size: 14px; border-bottom: 1px solid var(--border); }
        th { color: var(--text-sec); font-weight: 500; font-size: 13px; text-transform: uppercase; letter-spacing: 0.5px; }
        tr:last-child td { border-bottom: none; }
        
        .status-badge { display: inline-flex; align-items: center; padding: 4px 10px; border-radius: 12px; font-size: 12px; font-weight: 600; }
        .COMPLETED { background-color: rgba(52, 199, 89, 0.15); color: var(--acc-green); }
        .FAILED { background-color: rgba(255, 59, 48, 0.15); color: var(--acc-red); }
        .IN_PROGRESS { background-color: rgba(255, 204, 0, 0.15); color: var(--acc-yellow); }
        .SKIPPED { background-color: rgba(142, 142, 147, 0.15); color: var(--text-sec); }
        
        .icon { width: 20px; height: 20px; display: block; }
        .icon-sm { width: 16px; height: 16px; display: block; }
        .icon-lg { width: 28px; height: 28px; display: block; filter: drop-shadow(0 2px 4px rgba(0,0,0,0.1)); }
    </style>
    <script>
        function updateDashboard() {
            fetch('/api/status')
            .then(response => response.json())
            .then(data => {
                document.getElementById('cpu-stats').innerText = data.cpu_stats;
                document.getElementById('ram-stats').innerText = data.ram_stats;
                
                const statusCard = document.getElementById('status-card');
                const actionCard = document.getElementById('action-card');
                
                if (data.is_scanning) {
                    statusCard.innerHTML = `<svg class="icon-sm sp" style="color: var(--acc-blue);" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="2" x2="12" y2="6"></line><line x1="12" y1="18" x2="12" y2="22"></line><line x1="4.93" y1="4.93" x2="7.76" y2="7.76"></line><line x1="16.24" y1="16.24" x2="19.07" y2="19.07"></line><line x1="2" y1="12" x2="6" y2="12"></line><line x1="18" y1="12" x2="22" y2="12"></line><line x1="4.93" y1="19.07" x2="7.76" y2="16.24"></line><line x1="16.24" y1="4.93" x2="19.07" y2="7.76"></line></svg> Working`;
                    
                    actionCard.innerHTML = `<button class="btn btn-disabled" disabled>
                        <svg class="icon sp" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="2" x2="12" y2="6"></line><line x1="12" y1="18" x2="12" y2="22"></line><line x1="4.93" y1="4.93" x2="7.76" y2="7.76"></line><line x1="16.24" y1="16.24" x2="19.07" y2="19.07"></line><line x1="2" y1="12" x2="6" y2="12"></line><line x1="18" y1="12" x2="22" y2="12"></line><line x1="4.93" y1="19.07" x2="7.76" y2="16.24"></line><line x1="16.24" y1="4.93" x2="19.07" y2="7.76"></line></svg> Progress</button>`;
                } else {
                    statusCard.innerHTML = "Idle";
                    actionCard.innerHTML = `<form action="/start_scan" method="POST" style="margin:0;">
                        <button class="btn" id="start-btn" type="submit">
                        <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="5 3 19 12 5 21 5 3"></polygon></svg>
                        Start Scan</button></form>`;
                }

                let rowsHtml = '';
                data.rows.forEach(row => {
                    rowsHtml += `<tr>
                        <td style="font-weight: 500;">${row[1]}</td>
                        <td><span class="status-badge ${row[2]}">${row[2]}</span></td>
                        <td style="color: var(--text-sec);">${row[3]}</td>
                        <td style="color: var(--text-sec);">${row[4]}</td>
                        <td style="font-weight: 500;">${row[5]}</td>
                        <td style="color: var(--text-sec); font-size: 13px;">${row[6]}</td>
                    </tr>`;
                });
                document.getElementById('table-body').innerHTML = rowsHtml;
            });
        }
        
        setInterval(updateDashboard, 2000);
        
        window.onload = function() {
            updateDashboard();
        }
    </script>
</head>
<body>
    <div class="container">
        <h1>
            <svg class="icon-lg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="23 7 16 12 23 17 23 7"></polygon><rect x="1" y="5" width="15" height="14" rx="2" ry="2"></rect></svg>
            HEVC Transcoder
        </h1>
        <p class="subtitle">Hardware accelerated with Intel QSV. Scheduled natively between 01:00 and 07:00.</p>
        
        <div class="header-cards">
            <div class="card">
                <div class="card-header">
                    <svg class="icon-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="4" width="16" height="16" rx="2" ry="2"></rect><rect x="9" y="9" width="6" height="6"></rect><line x1="9" y1="1" x2="9" y2="4"></line><line x1="15" y1="1" x2="15" y2="4"></line><line x1="9" y1="20" x2="9" y2="23"></line><line x1="15" y1="20" x2="15" y2="23"></line><line x1="20" y1="9" x2="23" y2="9"></line><line x1="20" y1="14" x2="23" y2="14"></line><line x1="1" y1="9" x2="4" y2="9"></line><line x1="1" y1="14" x2="4" y2="14"></line></svg>
                    CPU Load
                </div>
                <div id="cpu-stats" class="card-value">-</div>
            </div>
            
            <div class="card">
                <div class="card-header">
                    <svg class="icon-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="4" y1="8" x2="20" y2="8"></line><line x1="4" y1="16" x2="20" y2="16"></line><line x1="8" y1="4" x2="8" y2="20"></line><line x1="16" y1="4" x2="16" y2="20"></line></svg>
                    RAM Usage
                </div>
                <div id="ram-stats" class="card-value" style="font-size: 16px;">-</div>
            </div>
            
            <div class="card">
                <div class="card-header">
                    <svg class="icon-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"></circle><polyline points="12 6 12 12 16 14"></polyline></svg>
                    Status
                </div>
                <div id="status-card" class="card-value">
                    Idle
                </div>
            </div>
            
            <div id="action-card" class="card" style="border:none; box-shadow:none; background:transparent; padding:0; flex: 0.5; justify-content:center; align-items:flex-end;">
            </div>
        </div>

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
</body>
</html>
"""

@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route("/api/status")
def status_api():
    cpu, ram = get_sys_stats()
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT * FROM conversions ORDER BY id DESC LIMIT 100")
    raw_rows = c.fetchall()
    conn.close()
    
    fmt_rows = []
    for r in raw_rows:
        fmt_rows.append((
            r[0], # id
            r[1], # filename
            r[5], # status
            format_size(r[3]), # old_size
            format_size(r[4]), # new_size
            format_size(r[3] - r[4]) if r[3] and r[4] else '-', # saved
            r[8][:16] if r[8] else '-' # finished_at
        ))
        
    return jsonify({
        "cpu_stats": cpu,
        "ram_stats": ram,
        "is_scanning": is_scanning,
        "rows": fmt_rows
    })

@app.route("/start_scan", methods=["POST"])
def manual_start():
    force_scan_event.set()
    time.sleep(1) # short wait to allow state change 
    return "<script>window.location.href='/';</script>"

if __name__ == '__main__':
    init_db()
    
    # Start the background conversion thread
    t = threading.Thread(target=scanner_loop, daemon=True)
    t.start()
    
    # Start the Flask web dashboard
    app.run(host='0.0.0.0', port=5050)
