# auto_download_from_drive

[中文文档](./zh_README.md)

A persistent background daemon that monitors **rclone-mounted remote directories** and automatically downloads **only new files** to a local destination. Comes with a web management panel providing real-time transfer progress via WebSocket.

---

## Architecture

The two components share no IPC — they communicate exclusively through files on disk:

```
[ Caddy (TLS reverse proxy) ]
         ↓ :5000
[ web-panel.service  (Gunicorn / gevent, 1 worker) ]
         ↓ reads
[ /opt/sync/  config.json · sync_state.json · active_transfers.json · sync.log ]
         ↑ writes
[ sync.service  (sync_daemon.py) ]
         ↓ rclone copy
[ rclone-pikpak.service  (FUSE mount) ]
         ↓
[ Remote cloud storage (e.g. PikPak) ]
```

| Component | Role |
|---|---|
| `sync_daemon.py` | Scans mounted paths on a timer, enqueues new files, downloads via `rclone copy`, persists state |
| `web_panel/app.py` | Flask + SocketIO panel; read-only except for `config.json` writes that trigger a daemon restart |

---

## Repository Layout

```
/
├── sync_daemon.py          ← daemon entry point
├── sync_daemon.service     ← systemd unit template (dev)
├── config.json             ← shared runtime configuration
├── sync_state.json         ← persistent file-status state (written by daemon)
├── active_transfers.json   ← live download registry (written by daemon)
├── sync.log                ← rotating log (written by daemon)
├── start.sh                ← one-shot install script (requires root)
web_panel/
├── app.py                  ← Flask application
├── rclone_monitor.py       ← polls rclone RC ports for per-transfer progress
├── requirements.txt
└── templates/index.html    ← single-page UI (Tailwind CSS, dark/light, SocketIO)
```

---

## Features

### Incremental Downloads & Baseline Management
On the first run (or when a new rule is added), all existing source files are marked `baseline` and skipped. Only files that appear **after** the initial scan are ever downloaded, preventing mass re-downloads of historical data.

### Multi-Rule Support
Configure multiple independent `source_path → dest_path` rules in `config.json`. Each rule has an `enabled` flag and can be toggled without restarting the daemon.

### Concurrent Downloads & Bandwidth Control
- Configurable worker thread pool via `max_concurrent_downloads`.
- Per-transfer speed cap via `bandwidth_limit_mbps` (`0` = unlimited).

### Automatic Retry
Failed downloads are re-queued automatically on subsequent scan cycles until `max_retry_count` is reached, at which point the file is marked `permanent_failed`.

### Automated Mount Refreshing
Periodically restarts the rclone mount systemd service to prevent stale-mount issues. The daemon pauses scanning, waits for all workers to go idle, restarts the service, confirms the mount is accessible, then resumes.

### State Persistence & Auto-Cleanup
All file states survive restarts via `sync_state.json` (with a `.bak` fallback for corruption recovery). When a file disappears from the source, its state entry is automatically pruned — local copies are never touched.

### Web Management Panel
Browser UI for:
- Real-time per-transfer progress (SocketIO push, 1 s interval)
- Configuration editing (writes `config.json` + triggers daemon restart)
- Log tail
- File-state overview and statistics

### Graceful Shutdown
On SIGTERM the daemon drains the pending queue, waits up to 300 s for in-flight downloads to finish, then saves state before exiting.

---

## File State Lifecycle

```
(first seen on startup)  → baseline          ← skipped, never downloaded
(newly detected)         → pending
pending → download OK    → synced
pending → download fail  → failed
failed  → retries > max  → permanent_failed   ← manual intervention required
```

State is keyed as `<rule_id>:<source_file_path>` inside `sync_state.json`.

---

## Requirements

- Linux + systemd (Debian/Ubuntu recommended)
- Python 3.11+
- `rclone` installed and configured
- An existing rclone FUSE mount managed by systemd (e.g. `rclone-pikpak.service`)
- Root access for initial installation

---

## Installation

Run the bundled install script as root. It will:
1. Stop and remove any previous installation
2. Copy project files to `/opt/sync/`
3. Create a dedicated `web-panel` system user
4. Install Python dependencies into a virtualenv
5. Write and enable `sync.service` and `web-panel.service`
6. Generate a placeholder `WEB_PANEL_API_KEY` in `/opt/sync/web_panel/.env`

```bash
sudo ./start.sh
```

### Post-Install Configuration (required)

**1. `/opt/sync/config.json` — core daemon settings**

Edit `rules` to set your actual source and destination paths, then enable each rule:

```bash
sudo nano /opt/sync/config.json
sudo systemctl restart sync.service
```

**2. `/opt/sync/web_panel/.env` — web panel authentication**

Set a strong API key and your public domain for CORS:

```bash
sudo nano /opt/sync/web_panel/.env
sudo systemctl restart web-panel.service
```

**3. Caddy reverse proxy (example)**

```caddy
panel.example.com {
    @allowed remote_ip YOUR.IP.ADDRESS
    handle @allowed {
        reverse_proxy 127.0.0.1:5000
    }
    respond 403
}
```

---

## Configuration Reference (`config.json`)

| Field | Type | Description |
|---|---|---|
| `scan_interval_seconds` | int | Interval between directory scans for new files (seconds) |
| `rclone_refresh_interval_seconds` | int | Interval between rclone mount service restarts (seconds) |
| `max_concurrent_downloads` | int | Number of parallel download worker threads |
| `max_retry_count` | int | Max retry attempts before marking a file `permanent_failed` |
| `bandwidth_limit_mbps` | float | Download speed cap in Mbps; `0` disables the limit |
| `rclone_command` | string | Path or name of the `rclone` binary (default: `"rclone"`) |
| `rclone_service_name` | string | systemd service name for the rclone FUSE mount (e.g. `"rclone-pikpak"`) |
| `rules` | array | List of sync rules (see below) |

### Rule fields

| Field | Type | Description |
|---|---|---|
| `source_path` | string | Absolute path to the rclone-mounted source directory |
| `dest_path` | string | Absolute path to the local download destination |
| `enabled` | bool | Set to `true` to activate the rule |

---

## Web Panel Environment Variables (`.env`)

| Variable | Default | Notes |
|---|---|---|
| `WEB_PANEL_API_KEY` | _(empty = dev mode)_ | Must be set in production |
| `WEB_PANEL_ALLOWED_ORIGINS` | `http://localhost,https://localhost` | Comma-separated CORS + SocketIO origins |
| `WEB_PANEL_SECRET_KEY` | auto-generated | Set explicitly to survive service restarts |
| `WEB_PANEL_SESSION_TTL_SECONDS` | `1800` | Sliding session lifetime (seconds) |
| `WEB_PANEL_LOG_LEVEL` | `INFO` | Python log level |

---

## Useful Commands

```bash
# Check daemon logs
sudo journalctl -u sync.service -f

# Check web panel logs
sudo journalctl -u web-panel.service -f
sudo tail -f /var/log/web-panel/error.log

# Reload config without full reinstall
sudo systemctl restart sync.service

# View live file state
cat /opt/sync/sync_state.json | python3 -m json.tool
```

---

## Notes

- **One-way sync only.** The daemon downloads new files; it never deletes local copies, never mirrors deletions, and never performs two-way sync.
- **Source-side deletions.** When a file is removed from the source, its state record is pruned automatically. The local downloaded copy is preserved.
- **Permissions.** The runtime user must have read access to `source_path` and write access to `dest_path`.
