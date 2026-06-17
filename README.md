# auto_download_from_drive

[![Python](https://img.shields.io/badge/Python-3.x-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![rclone](https://img.shields.io/badge/rclone-remote%20or%20mount-3F79E0)](https://rclone.org/)
[![systemd](https://img.shields.io/badge/systemd-service-FFB300)](https://systemd.io/)
[![Linux](https://img.shields.io/badge/Linux-Debian%20%2F%20Ubuntu-FCC624?logo=linux&logoColor=black)](https://kernel.org/)

[中文文档](./zh_README.md)

`auto_download_from_drive` is a lightweight Linux daemon driven entirely by [`sync_daemon.py`](./sync_daemon.py).

It watches one or more sources, records a baseline on first run, and only downloads files discovered later after their size and mtime are stable across two scans. Actual transfers are executed with `rclone copyto` so the source directory layout is preserved under the destination.

## Features

- one-way incremental download only
- supports local absolute paths like `/mnt/pikpak/My Pack`
- supports direct rclone remotes like `pikpak:My Pack`
- configurable concurrent downloads with retry handling
- preserves source subdirectories and avoids same-name file collisions
- waits for new files to stabilize before downloading
- scanning pauses while a download is active or queued
- periodic mount refresh through `systemctl restart`
- state persisted in JSON files under the working directory
- single `sync.log` file trimmed in place to the latest 24 hours
- native `systemd` `Type=notify` + watchdog support
- Telegram Bot menu with live download progress and rule state views
- per-transfer local Rclone RC ports for progress stats, avoiding `localhost:5572` conflicts

This is not bidirectional sync, mirror sync, or delete sync.

## Files

- `sync_daemon.py`: daemon runtime
- `config.json`: runtime config, auto-created if missing
- `sync_state.json`: persisted file state
- `sync.log`: single current log file, trimmed in place to the latest 24 hours
- `tests/`: unit tests

## Quick Start

### Local Development

Run directly:

```bash
python3 sync_daemon.py
```

The daemon will create `config.json` if it doesn't exist. Edit it to configure your sync rules:

```json
{
  "scan_interval_seconds": 300,
  "rclone_refresh_interval_seconds": 1800,
  "max_concurrent_downloads": 1,
  "max_retry_count": 5,
  "download_timeout_seconds": 0,
  "bandwidth_limit_mbps": 0,
  "rclone_command": "rclone",
  "rclone_service_name": "",
  "telegram": {
    "enabled": false,
    "bot_token": "",
    "chat_id": "",
    "message_thread_id": null,
    "poll_timeout_seconds": 25,
    "progress_refresh_seconds": 3,
    "progress_live_seconds": 120
  },
  "rclone_rc": {
    "host": "127.0.0.1",
    "port_min": 0,
    "port_max": 0,
    "request_timeout_seconds": 2
  },
  "rules": [
    {
      "source_path": "pikpak:My Pack",
      "dest_path": "/data/downloads",
      "enabled": true
    }
  ]
}
```

`dest_path` can also be a direct rclone remote, for example `"pikpak:Downloads"`.

### Production Deployment with systemd

1. Copy the daemon to your target directory (e.g., `/opt/sync`):

```bash
sudo mkdir -p /opt/sync
sudo cp sync_daemon.py /opt/sync/
sudo cp config.json /opt/sync/  # optional: if you have a pre-configured config
```

2. Create a systemd service file at `/etc/systemd/system/sync.service`:

```ini
[Unit]
Description=Auto Download Sync Daemon
After=network-online.target

[Service]
Type=notify
WorkingDirectory=/opt/sync
ExecStart=/usr/bin/python3 /opt/sync/sync_daemon.py
Restart=always
RestartSec=10
WatchdogSec=300

[Install]
WantedBy=multi-user.target
```

3. Enable and start the service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable sync.service
sudo systemctl start sync.service
```

4. Check status and logs:

```bash
sudo systemctl status sync.service
sudo journalctl -u sync.service -f
tail -f /opt/sync/sync.log
```

To restart after config changes:

```bash
sudo systemctl restart sync.service
```

## Config

| Field | Type | Description |
|---|---|---|
| `scan_interval_seconds` | int | delay between incremental scans |
| `rclone_refresh_interval_seconds` | int | delay between refresh cycles |
| `max_concurrent_downloads` | int | number of download workers; `1` shows a single live progress bar in Telegram, values above `1` show an active transfer list |
| `max_retry_count` | int | positive failure threshold before `permanent_failed` |
| `download_timeout_seconds` | int | total transfer timeout; `0` disables the daemon-side timeout |
| `bandwidth_limit_mbps` | number | `0` disables `--bwlimit`; otherwise passed as `XM` |
| `rclone_command` | string | `rclone` binary name or absolute path |
| `rclone_service_name` | string | systemd unit restarted during refresh; leave empty to disable service restart |
| `telegram` | object | Telegram Bot config for completion notifications, long-polling commands, inline buttons, and progress views |
| `rclone_rc` | object | per-download local Rclone RC config used to query `core/stats` progress |
| `rules` | array | source-to-destination rules |

Telegram fields:

| Field | Type | Description |
|---|---|---|
| `enabled` | bool | enables Telegram notifications and Bot long polling |
| `bot_token` | string | Telegram Bot API token |
| `chat_id` | string | target chat/channel/group id |
| `message_thread_id` | int/null | optional forum topic id; use `null` for normal chats |
| `poll_timeout_seconds` | int | long-poll timeout for `getUpdates`; default `25` |
| `progress_refresh_seconds` | int | live progress edit interval; default `3` |
| `progress_live_seconds` | int | maximum live refresh window after pressing `Downloading`; default `120` |

Rclone RC fields:

| Field | Type | Description |
|---|---|---|
| `host` | string | must be `127.0.0.1`; RC is never bound to a non-loopback address |
| `port_min` | int | first allowed RC port; `0` with `port_max=0` means allocate a temporary local port |
| `port_max` | int | last allowed RC port; `0` with `port_min=0` means allocate a temporary local port |
| `request_timeout_seconds` | int | timeout for daemon HTTP requests to each transfer's `core/stats`; default `2` |

Rule fields:

| Field | Type | Description |
|---|---|---|
| `source_path` | string | local absolute path or direct rclone remote |
| `dest_path` | string | local absolute destination path or direct rclone remote |
| `enabled` | bool | enables the rule; must be a JSON boolean, not a string |
| `id` | string | optional stable rule id; if omitted, one is derived from `source_path` + `dest_path` |

## How It Works

First run for a rule:

```text
existing files -> baseline
```

Rules use a stable id instead of the array index, so reordering `rules` does not reset state. Changing a rule's `source_path` or `dest_path` without an explicit `id` creates a new derived id and a new baseline.

Later scans:

```text
new files -> observed -> pending -> synced
                         |
                         -> failed -> permanent_failed
```

`observed` means the daemon has seen a new or changed non-baseline file but has not downloaded it yet. It becomes `pending` only after size and mtime are unchanged across the next scan.

Telegram:

- `/start`, `/status`, and `/menu` send the same inline menu.
- The menu has `Downloading` and `States` buttons.
- The daemon only responds to the configured `chat_id`.
- If `message_thread_id` is configured, outgoing messages continue to use that topic id.
- `Downloading` reads each active transfer's Rclone RC `core/stats`. With one worker it shows a text progress bar such as `██████░░░░ 63%`; with multiple workers it shows one line per active transfer with speed and ETA.
- `States` shows every rule, whether it is enabled and initialized, source/destination paths, file status counts, and the most recent scan check.

Destination layout:

```text
source: pikpak:Movies/A/movie.mkv
dest:   /data/downloads/Movies/A/movie.mkv
```

Refresh flow:

1. Wait until active and queued downloads are both zero.
2. Pause new scans.
3. Restart `rclone_service_name` if configured.
4. Probe all enabled sources until they are ready again.
5. Resume scanning.

## Security Notes

- Rclone RC is started only for the lifetime of each `rclone copyto` child process.
- Each child process gets its own `--rc-addr 127.0.0.1:<port>`, so the daemon does not depend on or conflict with the default `127.0.0.1:5572`.
- The daemon rejects non-loopback RC hosts and does not add `--rc-no-auth`.
- Runtime files created by the daemon (`config.json`, `sync_state.json`, `sync.log`) are restricted to owner read/write permissions.
- Keep Telegram bot tokens, chat ids, runtime state, active transfer files, and logs out of source control.

## Development

Minimal local check:

```bash
python3 -m py_compile sync_daemon.py
python3 -m unittest
python3 sync_daemon.py
```

## Updating Production

To update the daemon in production:

```bash
sudo systemctl stop sync.service
sudo cp sync_daemon.py /opt/sync/
sudo systemctl start sync.service
```

Your `config.json`, `sync_state.json`, and `sync.log` files are preserved.
