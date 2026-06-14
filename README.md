# auto_download_from_drive

[![Python](https://img.shields.io/badge/Python-3.x-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![rclone](https://img.shields.io/badge/rclone-remote%20or%20mount-3F79E0)](https://rclone.org/)
[![systemd](https://img.shields.io/badge/systemd-service-FFB300)](https://systemd.io/)
[![Linux](https://img.shields.io/badge/Linux-Debian%20%2F%20Ubuntu-FCC624?logo=linux&logoColor=black)](https://kernel.org/)

[中文文档](./zh_README.md)

`auto_download_from_drive` is a lightweight Linux daemon driven entirely by [`sync_daemon.py`](./sync_daemon.py).

It watches one or more sources, records a baseline on first run, and only downloads files discovered later. Actual transfers are executed with `rclone copy` using fixed transfer/retry/timeout defaults tuned for multi-threaded downloads.

## Features

- one-way incremental download only
- supports local mount paths like `/mnt/pikpak/My Pack`
- supports direct rclone remotes like `pikpak:My Pack`
- serialized downloads with retry and timeout handling
- scanning pauses while a download is active or queued
- periodic mount refresh through `systemctl restart`
- state persisted in JSON files under the working directory
- single `sync.log` file trimmed in place to the latest 24 hours
- native `systemd` `Type=notify` + watchdog support

This is not bidirectional sync, mirror sync, or delete sync.

## Files

- `sync_daemon.py`: daemon runtime
- `start.sh`: fresh install to `/opt/sync`
- `update.sh`: update `/opt/sync/sync_daemon.py` and refresh `sync.service`
- `config.json`: runtime config, auto-created if missing
- `sync_state.json`: persisted file state
- `runtime_status.json`: active/queued counters
- `active_transfers.json`: currently running `rclone copy` processes
- `sync.log`: single current log file, trimmed in place to the latest 24 hours

## Quick Start

Install:

```bash
sudo ./start.sh
```

Edit `/opt/sync/config.json`:

```json
{
  "scan_interval_seconds": 300,
  "rclone_refresh_interval_seconds": 1800,
  "max_concurrent_downloads": 1,
  "max_retry_count": 5,
  "bandwidth_limit_mbps": 0,
  "rclone_command": "rclone",
  "rclone_service_name": "",
  "rules": [
    {
      "source_path": "pikpak:My Pack",
      "dest_path": "/data/downloads",
      "enabled": true
    }
  ]
}
```

Restart:

```bash
sudo systemctl restart sync.service
```

Follow logs:

```bash
sudo journalctl -u sync.service -f
tail -f /opt/sync/sync.log
```

## Config

| Field | Type | Description |
|---|---|---|
| `scan_interval_seconds` | int | delay between incremental scans |
| `rclone_refresh_interval_seconds` | int | delay between refresh cycles |
| `max_concurrent_downloads` | int | kept for config compatibility; runtime forces single-file downloads |
| `max_retry_count` | int | positive failure threshold before `permanent_failed` |
| `bandwidth_limit_mbps` | number | `0` disables `--bwlimit`; otherwise passed as `XM` |
| `rclone_command` | string | `rclone` binary name or absolute path |
| `rclone_service_name` | string | systemd unit restarted during refresh; leave empty to disable service restart |
| `rules` | array | source-to-destination rules |

Rule fields:

| Field | Type | Description |
|---|---|---|
| `source_path` | string | local mount path or direct rclone remote |
| `dest_path` | string | local destination path |
| `enabled` | bool | enables the rule; must be a JSON boolean, not a string |

## How It Works

First run for a rule:

```text
existing files -> baseline
```

Changing a rule's `source_path` resets that rule's state and creates a new baseline for the new source.

Later scans:

```text
new files -> pending -> synced
                 |
                 -> failed -> permanent_failed
```

Refresh flow:

1. Wait until active and queued downloads are both zero.
2. Pause new scans.
3. Restart `rclone_service_name` if configured.
4. Probe all enabled sources until they are ready again.
5. Resume scanning.

## Development

Minimal local check:

```bash
python3 -m py_compile sync_daemon.py
python3 sync_daemon.py
```

## Updating

```bash
sudo ./update.sh
```

`update.sh` preserves `/opt/sync/config.json`.
