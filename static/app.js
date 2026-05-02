function switchTab(event, tabId) {
    if (event) {
        document.querySelectorAll('.nav-tab').forEach(t => t.classList.remove('active'));
        event.target.closest('.nav-tab').classList.add('active');
    }
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    document.getElementById(tabId).classList.add('active');
    
    if (tabId === 'stats') loadStats();
    if (tabId === 'settings') loadSettings();
}

function cancelScan() {
    if (confirm("Cancel the current transcoding job? The original file will be safely kept and the temporary file deleted.")) {
        fetch('/api/cancel', { method: 'POST' }).then(() => updateDashboard());
    }
}

function skipCurrentMedia() {
    if (confirm("Skip this media permanently? It will be written to DB and never transcoded again.")) {
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
                if (data.progress && data.progress.filename) {
                    progHtml = `
                        <div style="margin-top: 16px; font-size: 14px; background: linear-gradient(135deg, rgba(0, 122, 255, 0.05), rgba(52, 199, 89, 0.05)); padding: 16px; border-radius: 12px;">
                            <div style="display: flex; justify-content: space-between; margin-bottom: 8px; align-items: center;">
                                <span style="white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 70%; font-weight: 500;">${escapeHtml(data.progress.filename)}</span>
                                <span style="font-weight: 700; color: var(--color-blue);">${data.progress.progress}%</span>
                            </div>
                            <div style="width: 100%; height: 6px; background: var(--bg-tertiary); border-radius: 3px; overflow: hidden;">
                                <div style="width: ${data.progress.progress}%; height: 100%; background: var(--color-blue); transition: width 0.3s ease;"></div>
                            </div>
                            <div style="display: flex; justify-content: space-between; margin-top: 8px; color: var(--text-secondary); font-size: 12px; font-weight: 500;">
                                <span>Speed: ${data.progress.speed} • FPS: ${data.progress.fps}</span>
                                <span>ETA: ${data.progress.eta}</span>
                            </div>
                        </div>`;
                }

                statusCard.innerHTML = `<span style="display:flex; align-items:center; gap:8px;"><svg class="spinner" style="width: 18px; height: 18px; color: var(--color-blue);" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="2" x2="12" y2="6"></line><line x1="12" y1="18" x2="12" y2="22"></line><line x1="4.93" y1="4.93" x2="7.76" y2="7.76"></line><line x1="16.24" y1="16.24" x2="19.07" y2="19.07"></line><line x1="2" y1="12" x2="6" y2="12"></line><line x1="18" y1="12" x2="22" y2="12"></line><line x1="4.93" y1="19.07" x2="7.76" y2="16.24"></line><line x1="16.24" y1="4.93" x2="19.07" y2="7.76"></line></svg> Transcoding...</span>${progHtml}`;
                
                actionCard.innerHTML = `<div style="display:flex; flex-direction:column; gap:12px; width:100%;">
                    <button class="btn btn-secondary" onclick="skipCurrentMedia()" style="width: 100%;">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" style="width: 18px; height: 18px;"><path d="M5 12h14"></path><path d="M12 5v14"></path></svg>
                        Skip Forever
                    </button>
                    <button class="btn btn-danger" onclick="cancelScan()" style="width: 100%;">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" style="width: 18px; height: 18px;"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>
                        Cancel Job
                    </button>
                </div>`;
            } else {
                statusCard.innerHTML = '<span style="color: var(--text-secondary);">Idle</span>';
                actionCard.innerHTML = `<form action="/start_scan" method="POST" style="margin:0; width: 100%;">
                    <button class="btn btn-primary" id="start-btn" type="submit" style="width: 100%;">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" style="width: 18px; height: 18px;"><polygon points="5 3 19 12 5 21 5 3"></polygon></svg>
                        Start Scan
                    </button>
                </form>`;
            }

            let rowsHtml = '';
            data.rows.forEach(row => {
                const fileName = escapeHtml(row[1]);
                const statusStr = row[2] || 'UNKNOWN';
                const status = statusStr.toLowerCase().replace(/_/g, ' ');
                const errorLog = row[7] ? `<div style="margin-top: 12px; padding: 8px 12px; border-radius: 8px; font-size: 11px; font-family: monospace; background: rgba(255, 59, 48, 0.05); color: var(--color-red); word-break: break-word; border: 1px solid rgba(255,59,48,0.1);">${escapeHtml(row[7])}</div>` : '';

                rowsHtml += `
                    <div class="job-card">
                        <div class="job-card-header">
                            <div class="job-filename">${fileName}</div>
                            <span class="status-badge ${status}">${statusStr}</span>
                        </div>
                        <div class="job-card-body">
                            <div class="job-stat">
                                <span class="stat-label">Original</span>
                                <span class="stat-value">${row[3]}</span>
                            </div>
                            <div class="job-stat">
                                <span class="stat-label">Result</span>
                                <span class="stat-value">${row[4]}</span>
                            </div>
                            <div class="job-stat">
                                <span class="stat-label">Saved</span>
                                <span class="stat-value saved">${row[5]}</span>
                            </div>
                            <div class="job-stat">
                                <span class="stat-label">Finished</span>
                                <span class="stat-value secondary">${row[6]}</span>
                            </div>
                        </div>
                        ${errorLog}
                    </div>`;
            });
            document.getElementById('jobs-list').innerHTML = rowsHtml || '<div class="empty-state">No jobs yet</div>';

            let drivesHtml = '';
            data.drives.forEach(drive => {
                drivesHtml += `<div class="card">
                    <div style="display: flex; align-items: center; gap: 12px; margin-bottom: 12px;">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" style="width: 18px; height: 18px; color: var(--text-secondary);">
                            <rect x="2" y="2" width="20" height="8" rx="2" ry="2"></rect>
                            <rect x="2" y="14" width="20" height="8" rx="2" ry="2"></rect>
                            <line x1="6" y1="6" x2="6" y2="6"></line>
                            <line x1="6" y1="18" x2="6" y2="18"></line>
                        </svg>
                        <span style="font-size: 14px; font-weight: 500; color: var(--text-secondary);">${escapeHtml(drive.mount)}</span>
                    </div>
                    <div style="font-size: 24px; font-weight: 700; margin-bottom: 8px;">${drive.free} free</div>
                    <div style="color: var(--text-secondary); font-size: 13px; font-weight: 500;">Total: ${drive.total} • ${drive.perc} used</div>
                </div>`;
            });
            document.getElementById('drives-container').innerHTML = drivesHtml;
        });
}

function escapeHtml(value) {
    return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function renderStatsCharts(data) {
    const daily = data.daily || [];
    const activityMax = Math.max(...daily.map(day => day.total || 0), 1);
    const savingsMax = Math.max(...daily.map(day => day.saved_bytes || 0), 1);

    const activityBars = daily.map(day => {
        const total = day.total || 0;
        const barHeight = total > 0 ? Math.max((total / activityMax) * 140, 12) : 12;
        const segments = [
            { value: day.completed || 0, color: 'var(--color-green)' },
            { value: day.failed || 0, color: 'var(--color-red)' },
            { value: day.skipped || 0, color: 'var(--text-tertiary)' },
            { value: (day.cancelled || 0) + (day.perma_skipped || 0), color: 'var(--color-yellow)' },
        ].filter(segment => segment.value > 0).map(segment => {
            const percent = total > 0 ? (segment.value / total) * 100 : 0;
            return `<div style="height: ${percent}%; background: ${segment.color}; flex: 1;"></div>`;
        }).join('');

        return `
            <div class="chart-bar" style="height: 100%; justify-content: flex-end;">
                <div style="width: 100%; flex: 1; display: flex; flex-direction: column; justify-content: flex-end;">
                    <div style="width: 100%; height: ${barHeight}px; display: flex; flex-direction: column; border-radius: 4px; overflow: hidden; background: var(--bg-tertiary);">
                        ${segments || '<div style="height: 100%; background: rgba(127,127,127,0.12);"></div>'}
                    </div>
                </div>
                <div style="display: flex; flex-direction: column; align-items: center; height: 32px; justify-content: flex-start; margin-top: 4px;">
                    <span class="chart-label">${escapeHtml(day.label)}</span>
                    <span style="font-size: 10px; color: var(--text-tertiary); text-align: center; white-space: nowrap; line-height: 1.2;">${total} jobs</span>
                </div>
            </div>`;
    }).join('');

    const savingsBars = daily.map(day => {
        const savedBytes = day.saved_bytes || 0;
        const barHeight = savedBytes > 0 ? Math.max((savedBytes / savingsMax) * 140, 12) : 12;
        return `
            <div class="chart-bar" style="height: 100%; justify-content: flex-end;">
                <div style="width: 100%; flex: 1; display: flex; flex-direction: column; justify-content: flex-end;">
                    <div style="width: 100%; height: ${barHeight}px; background: var(--color-blue); border-radius: 4px;"></div>
                </div>
                <div style="display: flex; flex-direction: column; align-items: center; height: 32px; justify-content: flex-start; margin-top: 4px;">
                    <span class="chart-label">${escapeHtml(day.label)}</span>
                    <span style="font-size: 10px; color: var(--text-tertiary); text-align: center; white-space: nowrap; line-height: 1.2;">${day.saved}</span>
                </div>
            </div>`;
    }).join('');

    document.getElementById('activity-chart').innerHTML = activityBars || '<div class="subtle">No recent data yet.</div>';
    document.getElementById('savings-chart').innerHTML = savingsBars || '<div class="subtle">No recent data yet.</div>';

    const topSavings = data.top_savings || [];
    document.getElementById('top-savings-list').innerHTML = topSavings.length
        ? topSavings.map(item => `
            <li class="ranking-item">
                <div class="ranking-info">
                    <span class="ranking-title">${escapeHtml(item.filename)}</span>
                    <span class="ranking-meta">Saved ${item.saved} • ${escapeHtml(item.duration)} • ${item.attempts} attempt(s)</span>
                </div>
            </li>`).join('')
        : '<li style="padding: 16px 0; text-align: center; color: var(--text-secondary);">No completed conversions yet.</li>';

    const topErrors = data.top_errors || [];
    document.getElementById('top-errors-list').innerHTML = topErrors.length
        ? topErrors.map(item => `
            <li class="ranking-item">
                <div class="ranking-info">
                    <span class="ranking-title">${escapeHtml(item.message)}</span>
                    <span class="ranking-meta">${item.count} occurrence(s)</span>
                </div>
            </li>`).join('')
        : '<li style="padding: 16px 0; text-align: center; color: var(--text-secondary);">No failure data yet.</li>';
}

function loadStats() {
    fetch('/api/stats')
        .then(res => res.json())
        .then(data => {
            document.getElementById('total-saved').innerText = data.total_saved;
            document.getElementById('total-processed').innerText = data.total_processed;
            document.getElementById('total-failed').innerText = data.total_failed;
            document.getElementById('success-rate').innerText = data.success_rate;
            document.getElementById('avg-duration').innerText = data.avg_duration;
            document.getElementById('avg-saved').innerText = data.avg_saved;
            document.getElementById('retry-rate').innerText = data.retry_rate;
            document.getElementById('total-perma-skipped').innerText = data.total_perma_skipped;
            document.getElementById('total-retried-jobs').innerText = data.total_retried_jobs;
            renderStatsCharts(data);
        });
}

function loadSettings() {
    fetch('/api/settings')
        .then(res => res.json())
        .then(data => {
            const qSlider = document.getElementById('quality-slider');
            if (qSlider) qSlider.value = data.quality === '18' ? 3 : (data.quality === '23' ? 2 : 1);
            
            const startInput = document.getElementById('scan-start');
            if (startInput) startInput.value = data.scan_start_hour || 1;
            
            const endInput = document.getElementById('scan-end');
            if (endInput) endInput.value = data.scan_end_hour || 7;
            
            const windowEl = document.getElementById('auto-run-window');
            if (windowEl) {
                const s = String(data.scan_start_hour || 1).padStart(2, '0');
                const e = String(data.scan_end_hour || 7).padStart(2, '0');
                windowEl.innerText = `${s}:00 - ${e}:00`;
            }
            
            let dirHtml = '';
            data.directories.forEach(d => {
                dirHtml += `<li class="settings-item">
                    <span class="settings-item-path">${escapeHtml(d.path)}</span>
                    <button onclick="removeDir(${d.id})" class="btn btn-small" style="background: rgba(255, 59, 48, 0.15); color: var(--color-red); margin-left: 12px; flex-shrink: 0;">Remove</button>
                </li>`;
            });
            document.getElementById('dir-list').innerHTML = dirHtml || '<li style="padding: 12px 0; color: var(--text-secondary);">No directories configured</li>';
        });

    fetch('/api/perma_skipped')
        .then(res => res.json())
        .then(data => {
            let skipHtml = '';
            data.items.forEach(item => {
                skipHtml += `<li class="settings-item">
                    <div class="ranking-info" style="flex: 1; min-width: 0;">
                        <span class="ranking-title">${escapeHtml(item.filename)}</span>
                        <span class="ranking-meta">${escapeHtml(item.filepath)}</span>
                    </div>
                    <button onclick="restoreSkipped(${item.id})" class="btn btn-small btn-primary" style="margin-left: 12px; flex-shrink: 0;">Unskip</button>
                </li>`;
            });
            document.getElementById('skip-list').innerHTML = skipHtml || '<li style="padding: 12px 0; text-align: center; color: var(--text-secondary);">No permanently skipped items</li>';
        });
}

function saveSettings() {
    const val = document.getElementById('quality-slider').value;
    const q = val == 3 ? '18' : (val == 2 ? '23' : '28');
    const start = document.getElementById('scan-start').value || '1';
    const end = document.getElementById('scan-end').value || '7';
    
    fetch('/api/settings/quality', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ quality: q, scan_start_hour: start, scan_end_hour: end })
    });
}

function retryAllFailed() {
    if (confirm("Retry all previously FAILED jobs? They will be picked up on the next scan.")) {
        fetch('/api/retry_all_failed', { method: 'POST' })
            .then(res => res.json())
            .then(() => {
                alert("Failed jobs cleared. They will be retried when the scanner runs.");
                pollStats(); 
            });
    }
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
                    return `<div class="suggestion-item" onclick="selectDir(event, '${f.path.replace(/'/g, "\\'")}')">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path>
                        </svg>
                        ${escapeHtml(f.name)}
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
        if (drop) drop.style.display = 'none';
    }
});

function addDir(event) {
    event.preventDefault();
    const input = document.getElementById('new-dir');
    fetch('/api/settings/dir', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: input.value })
    }).then(() => {
        input.value = '';
        document.getElementById('dir-suggestions').style.display = 'none';
        loadSettings();
    });
}

function removeDir(id) {
    fetch('/api/settings/dir/' + id, { method: 'DELETE' })
        .then(() => loadSettings());
}

function restoreSkipped(id) {
    fetch('/api/perma_skipped/' + id, { method: 'DELETE' })
        .then(() => loadSettings());
}

// Auto-update dashboard
setInterval(() => {
    if (document.getElementById('dashboard').classList.contains('active')) {
        updateDashboard();
    }
}, 1500);

// Initial load
window.addEventListener('load', function() {
    updateDashboard();
    // Set active tab based on URL or default
    const firstTab = document.querySelector('.nav-tab');
    if (firstTab) firstTab.click();
});
