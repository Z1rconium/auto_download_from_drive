#!/usr/bin/env python3
import json
import logging
import os
import queue
import re
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
from hashlib import sha1


CONFIG_FILE = "config.json"
STATE_FILE = "sync_state.json"
LOG_FILE = "sync.log"
LOG_RETENTION_SECONDS = 24 * 60 * 60
LOG_PRUNE_INTERVAL_SECONDS = 60
LOG_TIMESTAMP_LENGTH = len("2026-06-14T00:00:00+0000")
LOG_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S%z"
ROTATED_LOG_NAME_RE = re.compile(rf"^{re.escape(LOG_FILE)}\.\d{{4}}-\d{{2}}-\d{{2}}_\d{{2}}$")
SERVICE_NAME = "rclone-pikpak"
FILE_STABILITY_SCAN_COUNT = 2
ENTRY_TYPE_FILE = "file"
ENTRY_TYPE_DIRECTORY = "directory"
TELEGRAM_MARKDOWN_PARSE_MODE = "MarkdownV2"
TELEGRAM_MARKDOWN_V2_ESCAPE_RE = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")
PRIVATE_FILE_MODE = 0o600
PRIVATE_FILE_OPEN_FLAGS = getattr(os, "O_NOFOLLOW", 0)

# Configuration limits
MAX_SCAN_INTERVAL_SECONDS = 86400
MAX_REFRESH_INTERVAL_SECONDS = 86400
MAX_CONCURRENT_DOWNLOADS = 100
MAX_RETRY_COUNT = 100
MAX_DOWNLOAD_TIMEOUT_SECONDS = 86400
MAX_BANDWIDTH_LIMIT_MBPS = 10000
MAX_QUEUE_SIZE = 10000
MAX_TELEGRAM_POLL_TIMEOUT_SECONDS = 120
MAX_TELEGRAM_PROGRESS_REFRESH_SECONDS = 300
MAX_TELEGRAM_PROGRESS_LIVE_SECONDS = 3600
MAX_TELEGRAM_RCLONE_STATS_WORKERS = 32
MAX_RCLONE_RC_REQUEST_TIMEOUT_SECONDS = 60
MAX_RCLONE_RC_PORT = 65535
TELEGRAM_LIVE_PROGRESS_INTERVAL_SECONDS = 1
RCLONE_RC_ADDRESS_IN_USE_MARKERS = (
    "address already in use",
    "address-in-use",
    "only one usage of each socket address",
)

DEFAULT_CONFIG = {
    "_comment": "Edit this file and restart the daemon.",
    "scan_interval_seconds": 300,
    "rclone_refresh_interval_seconds": 1800,
    "max_concurrent_downloads": 1,
    "max_retry_count": 5,
    "download_timeout_seconds": 0,
    "bandwidth_limit_mbps": 0,
    "rclone_command": "rclone",
    "rclone_service_name": SERVICE_NAME,
    "telegram": {
        "enabled": False,
        "bot_token": "",
        "chat_id": "",
        "message_thread_id": None,
        "poll_timeout_seconds": 25,
        "progress_refresh_seconds": 1,
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
            "source_path": "/path/to/mounted/rclone/folder",
            "dest_path": "/path/to/local/download/folder",
            "enabled": False,
            "_comment": "source_path and dest_path support local absolute paths like /mnt/pikpak/My Pack and rclone remotes like pikpak:My Pack. Set enabled=true after paths are valid."
        }
    ]
}


@dataclass
class Rule:
    rule_id: str
    legacy_rule_id: str
    source_path: str
    dest_path: str
    enabled: bool

    @property
    def source_kind(self) -> str:
        return "remote" if is_rclone_remote(self.source_path) else "local"


@dataclass(frozen=True)
class DownloadTask:
    rule_id: str
    source_file: str
    dest_path: str
    relative_path: str
    entry_type: str = ENTRY_TYPE_FILE


@dataclass(frozen=True)
class ActiveTransfer:
    rule_id: str
    source_file: str
    dest_file: str
    started_at: float
    process: subprocess.Popen
    rc_url: str


class EventType:
    SCAN = "SCAN"
    DOWNLOAD = "DOWNLOAD"
    NOTIFICATION = "NOTIFICATION"
    REFRESH = "REFRESH"
    ERROR = "ERROR"
    SYSTEM = "SYSTEM"


class EventTypeFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "event_type"):
            record.event_type = EventType.SYSTEM
        return True


def telegram_safe_truncate(message: str, max_chars: int = 4096) -> str:
    if len(message) <= max_chars:
        return message
    suffix = "..."
    return message[:max_chars - len(suffix)] + suffix


def telegram_markdown_v2_escape(value: object) -> str:
    return TELEGRAM_MARKDOWN_V2_ESCAPE_RE.sub(r"\\\1", str(value))


def telegram_markdown_v2_safe_truncate(message: str, max_chars: int = 4096) -> str:
    suffix = "\\.\\.\\."
    if len(message) <= max_chars:
        return message
    truncated = message[:max_chars - len(suffix)].rstrip("\\")
    return truncated + suffix


def ensure_private_file_permissions(path: Path) -> None:
    try:
        file_stat = path.lstat()
    except FileNotFoundError:
        return

    if not stat.S_ISREG(file_stat.st_mode):
        raise RuntimeError(f"{path} must be a regular file")
    os.chmod(path, PRIVATE_FILE_MODE)


def prepare_private_file(path: Path) -> None:
    ensure_private_file_permissions(path)
    if path.exists():
        return

    try:
        fd = os.open(
            str(path),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | PRIVATE_FILE_OPEN_FLAGS,
            PRIVATE_FILE_MODE,
        )
    except FileExistsError:
        ensure_private_file_permissions(path)
        return

    try:
        os.fchmod(fd, PRIVATE_FILE_MODE)
    finally:
        os.close(fd)


def open_private_text_for_write(path: Path):
    ensure_private_file_permissions(path)
    fd = os.open(
        str(path),
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | PRIVATE_FILE_OPEN_FLAGS,
        PRIVATE_FILE_MODE,
    )
    try:
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise RuntimeError(f"{path} must be a regular file")
        os.fchmod(fd, PRIVATE_FILE_MODE)
        return os.fdopen(fd, "w", encoding="utf-8")
    except Exception:
        os.close(fd)
        raise


def format_speed_binary(bytes_per_second: object) -> str:
    try:
        speed = float(bytes_per_second)
    except (TypeError, ValueError):
        speed = 0.0

    if speed < 0:
        speed = 0.0

    units = ["B/s", "KiB/s", "MiB/s", "GiB/s", "TiB/s"]
    unit_idx = 0
    while speed >= 1024 and unit_idx < len(units) - 1:
        speed /= 1024
        unit_idx += 1

    if unit_idx == 0:
        return f"{int(round(speed))} {units[unit_idx]}"
    return f"{speed:.1f} {units[unit_idx]}"


def format_eta(eta_seconds: object) -> str:
    if eta_seconds in (None, ""):
        return "--:--"
    try:
        total_seconds = int(float(eta_seconds))
    except (TypeError, ValueError):
        return str(eta_seconds)

    if total_seconds < 0:
        return "--:--"

    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def format_progress_bar(percentage: object, width: int = 10) -> str:
    try:
        percent = float(percentage)
    except (TypeError, ValueError):
        percent = 0.0

    percent = min(max(percent, 0.0), 100.0)
    filled = int(round((percent / 100.0) * width))
    filled = min(max(filled, 0), width)
    return f"{'█' * filled}{'░' * (width - filled)} {int(round(percent))}%"


def _transfer_stats_entry(stats: Dict[str, Any]) -> Dict[str, Any]:
    transferring = stats.get("transferring")
    if isinstance(transferring, list) and transferring:
        first = transferring[0]
        if isinstance(first, dict):
            return first
    return stats


def _first_positive_float(*values: object) -> Optional[float]:
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            return number
    return None


def _stats_percentage(stats: Dict[str, Any]) -> object:
    total_bytes = _first_positive_float(stats.get("totalBytes"), stats.get("size"))
    if total_bytes is not None:
        try:
            return (float(stats.get("bytes") or 0) / total_bytes) * 100.0
        except (TypeError, ValueError):
            pass

    entry = _transfer_stats_entry(stats)
    if "percentage" in entry:
        return entry.get("percentage")

    bytes_done = entry.get("bytes")
    entry_total_bytes = _first_positive_float(entry.get("size"), entry.get("totalBytes"))
    try:
        if entry_total_bytes is not None:
            return (float(bytes_done or 0) / entry_total_bytes) * 100.0
    except (TypeError, ValueError):
        pass
    return 0


def _stats_speed(stats: Dict[str, Any]) -> object:
    if "speed" in stats:
        return stats.get("speed")
    if "speedAvg" in stats:
        return stats.get("speedAvg")
    entry = _transfer_stats_entry(stats)
    return entry.get("speedAvg", entry.get("speed", 0))


def _stats_eta(stats: Dict[str, Any]) -> object:
    if "eta" in stats:
        return stats.get("eta")
    entry = _transfer_stats_entry(stats)
    return entry.get("eta")


def _display_path_tail(path: str) -> str:
    tail = Path(path.rstrip("/")).name
    return tail or path


def format_downloading_view(
    transfers: List[ActiveTransfer],
    stats_by_key: Dict[str, Dict[str, Any]],
    max_concurrent_downloads: int,
    queued_count: int,
    active_count: Optional[int] = None,
) -> str:
    display_active_count = len(transfers) if active_count is None else active_count
    lines = [
        "Downloading",
        f"Active: {display_active_count} | Queued: {queued_count}",
    ]

    if not transfers:
        lines.append("")
        if display_active_count > 0:
            lines.append("Download is starting or finalizing.")
        else:
            lines.append("No active downloads.")
        return telegram_safe_truncate("\n".join(lines))

    lines.append("")
    if max_concurrent_downloads == 1 and len(transfers) == 1:
        transfer = transfers[0]
        key = f"{transfer.rule_id}:{transfer.source_file}"
        stats = stats_by_key.get(key, {})
        lines.append(_display_path_tail(transfer.source_file))
        if stats.get("error"):
            lines.append(f"Progress unavailable: {stats['error']}")
        else:
            lines.append(format_progress_bar(_stats_percentage(stats)))
            lines.append(f"{format_speed_binary(_stats_speed(stats))} | ETA {format_eta(_stats_eta(stats))}")
        lines.append(f"Source: {transfer.source_file}")
        lines.append(f"Dest: {transfer.dest_file}")
        return telegram_safe_truncate("\n".join(lines))

    for transfer in transfers:
        key = f"{transfer.rule_id}:{transfer.source_file}"
        stats = stats_by_key.get(key, {})
        lines.append(f"- {_display_path_tail(transfer.source_file)}")
        if stats.get("error"):
            lines.append(f"  Progress unavailable: {stats['error']}")
            continue
        progress = format_progress_bar(_stats_percentage(stats), width=6).split()[-1]
        lines.append(f"  {format_speed_binary(_stats_speed(stats))} | ETA {format_eta(_stats_eta(stats))} | {progress}")
    preparing_count = display_active_count - len(transfers)
    if preparing_count > 0:
        lines.append(f"- {preparing_count} active download(s) preparing stats")

    return telegram_safe_truncate("\n".join(lines))


def _format_bool(value: object) -> str:
    return "yes" if value else "no"


def _format_markdown_field(label: str, value: object) -> str:
    return f"*{label}:* {telegram_markdown_v2_escape(value)}"


def _safe_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _format_last_check(last_check: object, markdown: bool = False) -> List[str]:
    if not isinstance(last_check, dict):
        if markdown:
            return [_format_markdown_field("Last check", "never")]
        return ["Last check: never"]

    kind = str(last_check.get("kind") or "unknown")
    success = _format_bool(last_check.get("success"))
    source_ready = _format_bool(last_check.get("source_ready"))
    duration = last_check.get("duration_seconds")
    duration_text = f"{duration}s" if duration is not None else "n/a"
    files_text = (
        f"discovered {_safe_int(last_check.get('discovered_files'))}, "
        f"new {_safe_int(last_check.get('new_files'))}, "
        f"removed {_safe_int(last_check.get('removed_files'))}, "
        f"queued {_safe_int(last_check.get('queued_files'))}"
    )

    if markdown:
        lines = [
            _format_markdown_field(
                "Last check",
                f"{kind} | success {success} | source ready {source_ready} | {duration_text}",
            ),
            _format_markdown_field("Files", files_text),
        ]
        error = str(last_check.get("error") or "").strip()
        if error:
            lines.append(_format_markdown_field("Error", error))
        return lines

    lines = [
        f"Last check: {kind} | success {success} | source ready {source_ready} | {duration_text}",
        f"Files: {files_text}",
    ]
    error = str(last_check.get("error") or "").strip()
    if error:
        lines.append(f"Error: {error}")
    return lines


def format_rules_state_view(rules: List[Rule], state: Dict[str, object], markdown: bool = True) -> str:
    rules_state = state.get("rules", {}) if isinstance(state, dict) else {}
    if not isinstance(rules_state, dict):
        rules_state = {}

    lines = ["*States*" if markdown else "States"]
    if not rules:
        lines.append("")
        lines.append(telegram_markdown_v2_escape("No rules configured.") if markdown else "No rules configured.")
        message = "\n".join(lines)
        if markdown:
            return telegram_markdown_v2_safe_truncate(message)
        return telegram_safe_truncate(message)

    for rule in rules:
        rule_state = rules_state.get(rule.rule_id, {})
        if not isinstance(rule_state, dict):
            rule_state = {}
        precomputed_counts = rule_state.get("status_counts")
        counts: Dict[str, int] = {}
        if isinstance(precomputed_counts, dict):
            for status, count in precomputed_counts.items():
                counts[str(status)] = _safe_int(count)
        else:
            files_state = rule_state.get("files", {})
            if not isinstance(files_state, dict):
                files_state = {}

            for file_state in files_state.values():
                if not isinstance(file_state, dict):
                    continue
                status = str(file_state.get("status") or "unknown")
                counts[status] = counts.get(status, 0) + 1

        status_parts = []
        for status in ("baseline", "observed", "pending", "failed", "permanent_failed", "synced"):
            status_parts.append(f"{status} {counts.get(status, 0)}")
        unknown_count = counts.get("unknown", 0)
        if unknown_count:
            status_parts.append(f"unknown {unknown_count}")

        lines.append("")
        if markdown:
            rule_status = "enabled" if rule.enabled else "disabled"
            lines.append(f"*{telegram_markdown_v2_escape(rule.rule_id)}* \\[{rule_status}\\]")
            lines.append(_format_markdown_field("Initialized", _format_bool(rule_state.get("initialized"))))
            lines.append(_format_markdown_field("Source", rule.source_path))
            lines.append(_format_markdown_field("Dest", rule.dest_path))
            lines.append(_format_markdown_field("Status", ", ".join(status_parts)))
            lines.extend(_format_last_check(rule_state.get("last_check"), markdown=True))
            continue

        lines.append(f"{rule.rule_id} [{'enabled' if rule.enabled else 'disabled'}]")
        lines.append(f"Initialized: {_format_bool(rule_state.get('initialized'))}")
        lines.append(f"Source: {rule.source_path}")
        lines.append(f"Dest: {rule.dest_path}")
        lines.append("Status: " + ", ".join(status_parts))
        lines.extend(_format_last_check(rule_state.get("last_check")))

    message = "\n".join(lines)
    if markdown:
        return telegram_markdown_v2_safe_truncate(message)
    return telegram_safe_truncate(message)


class RecentLogFileHandler(logging.FileHandler):
    def __init__(self, filename: Path, retention_seconds: int, encoding: str):
        super().__init__(filename, encoding=encoding)
        self.retention_seconds = retention_seconds

    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        self.prune()

    def prune(self) -> None:
        self.acquire()
        try:
            if self.stream is None:
                return

            self.flush()
            path = Path(self.baseFilename)
            if not path.exists():
                return

            cutoff_timestamp = datetime.now(timezone.utc).timestamp() - self.retention_seconds
            encoding = self.encoding or "utf-8"

            try:
                with path.open("r+", encoding=encoding) as fp:
                    lines = fp.readlines()
                    kept_lines = []
                    changed = False
                    keep_continuation = False

                    for line in lines:
                        logged_at = self._parse_log_time(line)
                        if logged_at is None:
                            if keep_continuation:
                                kept_lines.append(line)
                            else:
                                changed = True
                            continue

                        keep_continuation = logged_at.timestamp() >= cutoff_timestamp
                        if keep_continuation:
                            kept_lines.append(line)
                        else:
                            changed = True

                    if not changed:
                        return

                    fp.seek(0)
                    fp.writelines(kept_lines)
                    fp.truncate()
            except OSError:
                # Log pruning is non-critical, silently continue
                return

            try:
                self.stream.seek(0, os.SEEK_END)
            except (OSError, AttributeError):
                pass
        finally:
            self.release()

    def _parse_log_time(self, line: str) -> Optional[datetime]:
        if len(line) < LOG_TIMESTAMP_LENGTH:
            return None
        try:
            return datetime.strptime(line[:LOG_TIMESTAMP_LENGTH], LOG_TIMESTAMP_FORMAT)
        except ValueError:
            return None


def is_rclone_remote(path: str) -> bool:
    path = path.strip()
    if not path or path.startswith("/"):
        return False
    remote_name, separator, _remote_path = path.partition(":")
    if separator != ":" or not remote_name:
        return False
    return "/" not in remote_name and "\\" not in remote_name


class SyncDaemon:
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.config_path = self.base_dir / CONFIG_FILE
        self.state_path = self.base_dir / STATE_FILE
        self.log_path = self.base_dir / LOG_FILE

        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self.state_lock = threading.RLock()
        self.queue_lock = threading.Lock()
        self.download_scan_gate = threading.Lock()

        self.download_queue: queue.Queue[DownloadTask] = queue.Queue(maxsize=MAX_QUEUE_SIZE)
        self.queued_files = set()
        self.in_progress_files = set()
        self.active_downloads = 0
        self.workers: List[threading.Thread] = []

        self.config = {}
        self.rules: List[Rule] = []
        self.state: Dict[str, object] = {
            "schema_version": 1,
            "rules": {}
        }
        self.active_transfers: Dict[str, ActiveTransfer] = {}
        self.rc_port_lock = threading.Lock()
        self.reserved_rc_ports = set()
        self.telegram_thread: Optional[threading.Thread] = None
        self.telegram_update_offset: Optional[int] = None
        self.telegram_edit_lock = threading.Lock()
        self.telegram_live_progress_lock = threading.Lock()
        self.telegram_live_progress_sessions: Dict[Tuple[str, int], int] = {}
        self.systemd_notify_socket = os.environ.get("NOTIFY_SOCKET", "").strip()
        watchdog_usec = os.environ.get("WATCHDOG_USEC", "").strip()
        self.systemd_watchdog_interval = self._parse_watchdog_interval(watchdog_usec)
        self.watchdog_thread: Optional[threading.Thread] = None
        self.logger = self._setup_logging()
        self.remove_rotated_log_files()

    def _setup_logging(self) -> logging.Logger:
        logger = logging.getLogger("sync_daemon")
        logger.disabled = False
        logger.setLevel(logging.INFO)
        for handler in logger.handlers:
            handler.close()
        logger.handlers.clear()

        prepare_private_file(self.log_path)

        formatter = logging.Formatter(
            "%(asctime)s %(levelname)s %(event_type)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )

        event_filter = EventTypeFilter()

        stdout_handler = logging.StreamHandler(sys.stdout)
        stdout_handler.setFormatter(formatter)
        stdout_handler.addFilter(event_filter)

        file_handler = RecentLogFileHandler(
            self.log_path,
            encoding="utf-8",
            retention_seconds=LOG_RETENTION_SECONDS,
        )
        file_handler.setFormatter(formatter)
        file_handler.addFilter(event_filter)
        file_handler.prune()

        logger.addHandler(stdout_handler)
        logger.addHandler(file_handler)

        return logger

    @staticmethod
    def _path_key(path: str) -> str:
        return os.path.abspath(path)

    def _is_path_within(self, path: str, parent_path: str) -> bool:
        try:
            return os.path.commonpath([self._path_key(path), self._path_key(parent_path)]) == self._path_key(parent_path)
        except ValueError:
            return False

    def _relative_path_for_source_file(self, rule: Rule, source_file: str) -> str:
        if is_rclone_remote(rule.source_path):
            return self._relative_remote_path(rule.source_path, source_file)
        return os.path.relpath(source_file, rule.source_path)

    def _relative_remote_path(self, source_root: str, source_file: str) -> str:
        source_root = source_root.rstrip("/")
        if source_file == source_root:
            return ""
        prefix = f"{source_root}/" if not source_root.endswith(":") else source_root
        if source_file.startswith(prefix):
            relative_path = source_file[len(prefix):]
        else:
            relative_path = source_file.split(":", 1)[-1].lstrip("/")
        return relative_path

    def _validated_relative_path_parts(self, relative_path: str) -> Tuple[str, ...]:
        path = Path(relative_path)

        if path.is_absolute():
            raise ValueError(f"relative path must not be absolute: {relative_path}")

        for part in path.parts:
            if part in ("", ".", ".."):
                raise ValueError(f"invalid path component: {part}")
            if ':' in part:
                raise ValueError(f"invalid path component (contains colon): {part}")

        return path.parts

    def _dest_file_path(self, dest_root: str, relative_path: str) -> Path:
        path_parts = self._validated_relative_path_parts(relative_path)
        dest_root_path = Path(dest_root).resolve()
        dest_file = dest_root_path.joinpath(*path_parts)

        try:
            dest_file_resolved = dest_file.resolve()
            dest_file_resolved.relative_to(dest_root_path)
        except (ValueError, RuntimeError):
            raise ValueError(f"destination path escapes root: {relative_path}")

        return dest_file

    def _dest_file_target(self, dest_root: str, relative_path: str) -> str:
        if is_rclone_remote(dest_root):
            path_parts = self._validated_relative_path_parts(relative_path)
            return self._join_remote_path(dest_root, "/".join(path_parts))
        return str(self._dest_file_path(dest_root, relative_path))

    def _dest_directory_target(self, dest_root: str, relative_path: str) -> str:
        if is_rclone_remote(dest_root):
            path_parts = self._validated_relative_path_parts(relative_path) if relative_path else ()
            if not path_parts:
                return dest_root.rstrip("/")
            return self._join_remote_path(dest_root, "/".join(path_parts))
        if not relative_path:
            return str(Path(dest_root).resolve())
        return str(self._dest_file_path(dest_root, relative_path))

    def _dest_parent_target(self, dest_root: str, relative_path: str) -> str:
        path_parts = self._validated_relative_path_parts(relative_path)
        parent_relative_path = "/".join(path_parts[:-1])
        return self._dest_directory_target(dest_root, parent_relative_path)

    def _normalized_relative_path(self, relative_path: str) -> str:
        return str(relative_path).replace("\\", "/").strip("/")

    def _relative_parent_paths(self, relative_path: str) -> List[str]:
        normalized = self._normalized_relative_path(relative_path)
        if not normalized:
            return []
        parts = [part for part in normalized.split("/") if part]
        return ["/".join(parts[:idx]) for idx in range(1, len(parts))]

    def _is_relative_path_under_directory(self, relative_path: str, directory_relative_path: str) -> bool:
        relative = self._normalized_relative_path(relative_path)
        directory = self._normalized_relative_path(directory_relative_path)
        return bool(relative and directory and relative != directory and relative.startswith(f"{directory}/"))

    def _directory_task_relative_paths(self, files_state: Dict[str, object]) -> List[str]:
        directories: List[str] = []
        for entry_state in files_state.values():
            if not isinstance(entry_state, dict):
                continue
            if entry_state.get("entry_type", ENTRY_TYPE_FILE) != ENTRY_TYPE_DIRECTORY:
                continue
            if entry_state.get("status") not in ("observed", "pending", "failed", "permanent_failed"):
                continue
            relative_path = str(entry_state.get("relative_path") or "").strip()
            if relative_path:
                directories.append(relative_path)
        directories.sort(key=lambda value: value.count("/"))
        return directories

    def _is_covered_by_directory_task(self, relative_path: str, directory_relative_paths: List[str]) -> bool:
        return any(
            self._is_relative_path_under_directory(relative_path, directory_relative_path)
            for directory_relative_path in directory_relative_paths
        )

    def _is_covered_by_other_directory_task(
        self,
        relative_path: str,
        directory_relative_paths: List[str],
    ) -> bool:
        relative = self._normalized_relative_path(relative_path)
        return any(
            self._normalized_relative_path(directory_relative_path) != relative
            and self._is_relative_path_under_directory(relative, directory_relative_path)
            for directory_relative_path in directory_relative_paths
        )

    def _entry_type_for_state(self, file_state: Dict[str, object]) -> str:
        entry_type = str(file_state.get("entry_type") or ENTRY_TYPE_FILE)
        if entry_type == ENTRY_TYPE_DIRECTORY:
            return ENTRY_TYPE_DIRECTORY
        return ENTRY_TYPE_FILE

    def _rule_config_id(self, _idx: int, item: dict) -> str:
        configured_id = str(item.get("id", "")).strip()
        if configured_id:
            return configured_id

        source_path = str(item.get("source_path", "")).strip()
        dest_path = str(item.get("dest_path", "")).strip()
        digest = sha1(f"{source_path}\0{dest_path}".encode("utf-8")).hexdigest()[:12]
        return f"rule_{digest}"

    def prune_log_file(self) -> None:
        for handler in self.logger.handlers:
            if isinstance(handler, RecentLogFileHandler):
                handler.prune()

    def remove_rotated_log_files(self) -> None:
        removed = 0
        for path in self.base_dir.iterdir():
            if not path.is_file() or not ROTATED_LOG_NAME_RE.fullmatch(path.name):
                continue
            try:
                path.unlink()
                removed += 1
            except OSError as exc:
                self.log_error(EventType.ERROR, "failed to remove rotated log file", file=str(path), error=str(exc))

        if removed > 0:
            self.log_event(EventType.SYSTEM, "rotated log files removed", count=removed)

    def log_event(self, event_type: str, message: str, **fields: object) -> None:
        payload = {"message": message}
        if fields:
            payload.update(fields)
        self.logger.info(json.dumps(payload, ensure_ascii=False), extra={"event_type": event_type})

    def log_error(self, event_type: str, message: str, **fields: object) -> None:
        payload = {"message": message}
        if fields:
            payload.update(fields)
        self.logger.error(json.dumps(payload, ensure_ascii=False), extra={"event_type": event_type})

    def _parse_watchdog_interval(self, watchdog_usec: str) -> Optional[float]:
        if not watchdog_usec:
            return None
        try:
            interval_seconds = int(watchdog_usec) / 1_000_000
        except ValueError:
            return None
        if interval_seconds <= 0:
            return None
        return max(interval_seconds / 2, 1.0)

    def _systemd_notify(self, *parts: str) -> None:
        if not self.systemd_notify_socket:
            return
        address = self.systemd_notify_socket
        if address.startswith("@"):
            address = "\0" + address[1:]
        payload = "\n".join(part for part in parts if part)
        if not payload:
            return
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
                sock.connect(address)
                sock.sendall(payload.encode("utf-8"))
        except OSError:
            pass

    def _watchdog_loop(self) -> None:
        interval = self.systemd_watchdog_interval
        if interval is None:
            return

        while not self.stop_event.wait(interval):
            active_downloads, queued_downloads = self.get_download_counters()
            self._systemd_notify(
                "WATCHDOG=1",
                f"STATUS=running active={active_downloads} queued={queued_downloads} paused={self.pause_event.is_set()}",
            )

    def start_systemd_watchdog(self) -> None:
        if self.systemd_watchdog_interval is None:
            return
        self.watchdog_thread = threading.Thread(target=self._watchdog_loop, name="systemd-watchdog", daemon=True)
        self.watchdog_thread.start()

    def get_download_counters(self) -> Tuple[int, int]:
        with self.queue_lock:
            return self.active_downloads, self.download_queue.qsize()

    def ensure_config(self) -> bool:
        if self.config_path.exists():
            ensure_private_file_permissions(self.config_path)
            return True

        with open_private_text_for_write(self.config_path) as fp:
            json.dump(DEFAULT_CONFIG, fp, indent=2)
            fp.write("\n")

        print(
            f"{CONFIG_FILE} created at {self.config_path}. "
            "Please edit it and restart the daemon.",
            file=sys.stderr,
        )
        return False

    def load_config(self) -> None:
        ensure_private_file_permissions(self.config_path)
        with self.config_path.open("r", encoding="utf-8") as fp:
            cfg = json.load(fp)

        scan_interval = int(cfg.get("scan_interval_seconds", 300))
        refresh_interval = int(cfg.get("rclone_refresh_interval_seconds", 1800))
        configured_max_workers = int(cfg.get("max_concurrent_downloads", 1))
        max_retry_count = int(cfg.get("max_retry_count", 5))
        download_timeout = int(cfg.get("download_timeout_seconds", 0))
        bandwidth_limit = float(cfg.get("bandwidth_limit_mbps", 0))
        rclone_command = str(cfg.get("rclone_command", "rclone"))
        rclone_service = str(cfg.get("rclone_service_name", SERVICE_NAME))
        telegram = self._load_telegram_config(cfg.get("telegram", {}))
        rclone_rc = self._load_rclone_rc_config(cfg.get("rclone_rc", {}))

        # Validate lower bounds
        if scan_interval <= 0:
            raise ValueError("scan_interval_seconds must be > 0")
        if refresh_interval <= 0:
            raise ValueError("rclone_refresh_interval_seconds must be > 0")
        if configured_max_workers <= 0:
            raise ValueError("max_concurrent_downloads must be > 0")
        if max_retry_count <= 0:
            raise ValueError("max_retry_count must be > 0")
        if download_timeout < 0:
            raise ValueError("download_timeout_seconds must be >= 0")
        if bandwidth_limit < 0:
            raise ValueError("bandwidth_limit_mbps must be >= 0")

        # Validate upper bounds
        if scan_interval > MAX_SCAN_INTERVAL_SECONDS:
            raise ValueError(f"scan_interval_seconds must be <= {MAX_SCAN_INTERVAL_SECONDS}")
        if refresh_interval > MAX_REFRESH_INTERVAL_SECONDS:
            raise ValueError(f"rclone_refresh_interval_seconds must be <= {MAX_REFRESH_INTERVAL_SECONDS}")
        if configured_max_workers > MAX_CONCURRENT_DOWNLOADS:
            raise ValueError(f"max_concurrent_downloads must be <= {MAX_CONCURRENT_DOWNLOADS}")
        if max_retry_count > MAX_RETRY_COUNT:
            raise ValueError(f"max_retry_count must be <= {MAX_RETRY_COUNT}")
        if download_timeout > MAX_DOWNLOAD_TIMEOUT_SECONDS:
            raise ValueError(f"download_timeout_seconds must be <= {MAX_DOWNLOAD_TIMEOUT_SECONDS}")
        if bandwidth_limit > MAX_BANDWIDTH_LIMIT_MBPS:
            raise ValueError(f"bandwidth_limit_mbps must be <= {MAX_BANDWIDTH_LIMIT_MBPS}")


        rules_cfg = cfg.get("rules", [])
        if not isinstance(rules_cfg, list):
            raise ValueError("rules must be a list")

        rules = []
        seen_rule_ids = set()
        for idx, item in enumerate(rules_cfg):
            if not isinstance(item, dict):
                raise ValueError(f"rules[{idx}] must be an object")
            source_path = str(item.get("source_path", "")).strip()
            dest_path = str(item.get("dest_path", "")).strip()
            enabled = item.get("enabled", False)
            if not isinstance(enabled, bool):
                raise ValueError(f"rules[{idx}].enabled must be a boolean")
            if not source_path or not dest_path:
                raise ValueError(f"rules[{idx}] source_path/dest_path must be non-empty")
            rule_id = self._rule_config_id(idx, item)
            if rule_id in seen_rule_ids:
                raise ValueError(f"duplicate rule id: {rule_id}")
            seen_rule_ids.add(rule_id)
            rules.append(
                Rule(
                    rule_id=rule_id,
                    legacy_rule_id=f"rule_{idx}",
                    source_path=source_path,
                    dest_path=dest_path,
                    enabled=enabled,
                )
            )

        self.config = {
            "scan_interval_seconds": scan_interval,
            "rclone_refresh_interval_seconds": refresh_interval,
            "max_concurrent_downloads": configured_max_workers,
            "max_retry_count": max_retry_count,
            "download_timeout_seconds": download_timeout,
            "bandwidth_limit_mbps": bandwidth_limit,
            "rclone_command": rclone_command,
            "rclone_service_name": rclone_service,
            "telegram": telegram,
            "rclone_rc": rclone_rc,
        }
        self.rules = rules

    def _load_telegram_config(self, telegram_cfg: object) -> Dict[str, Union[bool, str, int, None]]:
        if telegram_cfg is None:
            telegram_cfg = {}
        if not isinstance(telegram_cfg, dict):
            raise ValueError("telegram must be an object")

        enabled = telegram_cfg.get("enabled", False)
        if not isinstance(enabled, bool):
            raise ValueError("telegram.enabled must be a boolean")

        bot_token = str(telegram_cfg.get("bot_token", "")).strip()
        chat_id = str(telegram_cfg.get("chat_id", "")).strip()
        poll_timeout = int(telegram_cfg.get("poll_timeout_seconds", 25))
        progress_refresh = int(
            telegram_cfg.get("progress_refresh_seconds", TELEGRAM_LIVE_PROGRESS_INTERVAL_SECONDS)
        )
        progress_live = int(telegram_cfg.get("progress_live_seconds", 120))
        message_thread_id_raw = telegram_cfg.get("message_thread_id", None)
        message_thread_id = None

        if poll_timeout <= 0:
            raise ValueError("telegram.poll_timeout_seconds must be > 0")
        if poll_timeout > MAX_TELEGRAM_POLL_TIMEOUT_SECONDS:
            raise ValueError(f"telegram.poll_timeout_seconds must be <= {MAX_TELEGRAM_POLL_TIMEOUT_SECONDS}")
        if progress_refresh <= 0:
            raise ValueError("telegram.progress_refresh_seconds must be > 0")
        if progress_refresh > MAX_TELEGRAM_PROGRESS_REFRESH_SECONDS:
            raise ValueError(
                f"telegram.progress_refresh_seconds must be <= {MAX_TELEGRAM_PROGRESS_REFRESH_SECONDS}"
            )
        progress_refresh = min(progress_refresh, TELEGRAM_LIVE_PROGRESS_INTERVAL_SECONDS)
        if progress_live < 0:
            raise ValueError("telegram.progress_live_seconds must be >= 0")
        if progress_live > MAX_TELEGRAM_PROGRESS_LIVE_SECONDS:
            raise ValueError(f"telegram.progress_live_seconds must be <= {MAX_TELEGRAM_PROGRESS_LIVE_SECONDS}")

        if message_thread_id_raw not in (None, ""):
            try:
                message_thread_id = int(message_thread_id_raw)
            except (TypeError, ValueError) as exc:
                raise ValueError("telegram.message_thread_id must be an integer or null") from exc
            if message_thread_id <= 0:
                raise ValueError("telegram.message_thread_id must be > 0")

        if enabled and (not bot_token or not chat_id):
            raise ValueError("telegram.bot_token and telegram.chat_id are required when telegram.enabled=true")

        return {
            "enabled": enabled,
            "bot_token": bot_token,
            "chat_id": chat_id,
            "message_thread_id": message_thread_id,
            "poll_timeout_seconds": poll_timeout,
            "progress_refresh_seconds": progress_refresh,
            "progress_live_seconds": progress_live,
        }

    def _load_rclone_rc_config(self, rclone_rc_cfg: object) -> Dict[str, Union[str, int]]:
        if rclone_rc_cfg is None:
            rclone_rc_cfg = {}
        if not isinstance(rclone_rc_cfg, dict):
            raise ValueError("rclone_rc must be an object")

        host = str(rclone_rc_cfg.get("host", "127.0.0.1")).strip()
        port_min = int(rclone_rc_cfg.get("port_min", 0))
        port_max = int(rclone_rc_cfg.get("port_max", 0))
        request_timeout = int(rclone_rc_cfg.get("request_timeout_seconds", 2))

        if host != "127.0.0.1":
            raise ValueError("rclone_rc.host must be 127.0.0.1")
        if port_min < 0 or port_max < 0:
            raise ValueError("rclone_rc.port_min and rclone_rc.port_max must be >= 0")
        if port_min > MAX_RCLONE_RC_PORT or port_max > MAX_RCLONE_RC_PORT:
            raise ValueError(f"rclone_rc ports must be <= {MAX_RCLONE_RC_PORT}")
        if (port_min == 0) != (port_max == 0):
            raise ValueError("rclone_rc.port_min and rclone_rc.port_max must both be 0 or both be non-zero")
        if port_min and port_min > port_max:
            raise ValueError("rclone_rc.port_min must be <= rclone_rc.port_max")
        if request_timeout <= 0:
            raise ValueError("rclone_rc.request_timeout_seconds must be > 0")
        if request_timeout > MAX_RCLONE_RC_REQUEST_TIMEOUT_SECONDS:
            raise ValueError(
                f"rclone_rc.request_timeout_seconds must be <= {MAX_RCLONE_RC_REQUEST_TIMEOUT_SECONDS}"
            )

        return {
            "host": host,
            "port_min": port_min,
            "port_max": port_max,
            "request_timeout_seconds": request_timeout,
        }

    def load_state(self) -> None:
        if not self.state_path.exists():
            self.save_state()
            return

        ensure_private_file_permissions(self.state_path)
        try:
            with self.state_path.open("r", encoding="utf-8") as fp:
                self.state = json.load(fp)
        except Exception as exc:
            backup_path = self.state_path.with_suffix(".json.bak")
            self.log_error(EventType.ERROR, "failed to load state, trying backup", error=str(exc))
            if backup_path.exists():
                ensure_private_file_permissions(backup_path)
                with backup_path.open("r", encoding="utf-8") as fp:
                    self.state = json.load(fp)
                self.log_event(EventType.SYSTEM, "state restored from backup", backup=str(backup_path))
            else:
                self.state = {"schema_version": 1, "rules": {}}

        if "rules" not in self.state or not isinstance(self.state["rules"], dict):
            self.state["rules"] = {}

    def save_state(self) -> None:
        with self.state_lock:
            tmp_path = self.state_path.with_suffix(".json.tmp")
            bak_path = self.state_path.with_suffix(".json.bak")

            if self.state_path.exists():
                try:
                    if bak_path.exists():
                        bak_path.unlink()
                    os.replace(self.state_path, bak_path)
                    ensure_private_file_permissions(bak_path)
                except OSError as exc:
                    self.log_error(EventType.ERROR, "failed to create state backup", error=str(exc))

            with open_private_text_for_write(tmp_path) as fp:
                json.dump(self.state, fp, indent=2)
                fp.write("\n")
            os.replace(tmp_path, self.state_path)
            ensure_private_file_permissions(self.state_path)

    def _signal_handler(self, signum: int, _frame: object) -> None:
        # Signal handlers should only set flags to avoid calling non-async-safe functions
        self.stop_event.set()

    def install_signal_handlers(self) -> None:
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def run(self) -> int:
        if not self.ensure_config():
            return 0

        try:
            self.load_config()
        except Exception as exc:
            self.log_error(EventType.ERROR, "invalid config", error=str(exc))
            return 1

        self.load_state()
        self._initialize_rule_state()

        self.install_signal_handlers()
        self.start_telegram_bot()
        self.bootstrap_scan()
        self.start_workers()
        self.start_systemd_watchdog()

        last_scan = 0.0
        last_refresh = time.time()
        last_log_prune = time.time()
        scan_blocked_logged = False

        self.log_event(EventType.SYSTEM, "daemon started")
        self._systemd_notify("READY=1", "STATUS=daemon started")

        try:
            while not self.stop_event.is_set():
                now = time.time()

                if now - last_refresh >= self.config["rclone_refresh_interval_seconds"]:
                    self.refresh_mount()
                    last_refresh = time.time()

                if now - last_log_prune >= LOG_PRUNE_INTERVAL_SECONDS:
                    self.prune_log_file()
                    last_log_prune = now

                if not self.pause_event.is_set() and now - last_scan >= self.config["scan_interval_seconds"]:
                    active_downloads, queued_downloads = self.get_download_counters()
                    if active_downloads > 0 or queued_downloads > 0:
                        if not scan_blocked_logged:
                            self.log_event(
                                EventType.SCAN,
                                "scan skipped because download work is active",
                                active_downloads=active_downloads,
                                queued_downloads=queued_downloads,
                            )
                            scan_blocked_logged = True
                    elif self.download_scan_gate.acquire(blocking=False):
                        try:
                            self.incremental_scan()
                            self.enqueue_retry_candidates()
                            last_scan = time.time()
                            scan_blocked_logged = False
                        finally:
                            self.download_scan_gate.release()
                    else:
                        if not scan_blocked_logged:
                            self.log_event(EventType.SCAN, "scan skipped because download is starting")
                            scan_blocked_logged = True

                time.sleep(1)
        finally:
            # Handle cleanup in main thread (signal-safe)
            if self.stop_event.is_set():
                self.log_event(EventType.SYSTEM, "shutdown initiated")
            self.shutdown()

        return 0

    def _initialize_rule_state(self) -> None:
        with self.state_lock:
            rules_state = self.state["rules"]
            for rule in self.rules:
                legacy_state = rules_state.get(rule.legacy_rule_id)
                if (
                    rule.rule_id not in rules_state
                    and rule.legacy_rule_id != rule.rule_id
                    and isinstance(legacy_state, dict)
                    and legacy_state.get("source_path") == rule.source_path
                ):
                    rules_state[rule.rule_id] = rules_state.pop(rule.legacy_rule_id)
                    self.log_event(
                        EventType.SYSTEM,
                        "rule state migrated to stable id",
                        legacy_rule_id=rule.legacy_rule_id,
                        rule_id=rule.rule_id,
                    )

                if rule.rule_id not in rules_state:
                    rules_state[rule.rule_id] = {
                        "source_path": rule.source_path,
                        "dest_path": rule.dest_path,
                        "enabled": rule.enabled,
                        "initialized": False,
                        "last_check": None,
                        "files": {},
                    }
                else:
                    existing = rules_state[rule.rule_id]
                    if existing.get("source_path") != rule.source_path:
                        rules_state[rule.rule_id] = {
                            "source_path": rule.source_path,
                            "dest_path": rule.dest_path,
                            "enabled": rule.enabled,
                            "initialized": False,
                            "last_check": None,
                            "files": {},
                        }
                    else:
                        existing["dest_path"] = rule.dest_path
                        existing["enabled"] = rule.enabled
                        if not isinstance(existing.get("files"), dict):
                            existing["files"] = {}
                        existing.setdefault("initialized", False)
                        existing.setdefault("last_check", None)
                for source_file, file_state in rules_state[rule.rule_id]["files"].items():
                    if not isinstance(file_state, dict):
                        continue
                    file_state.setdefault("entry_type", ENTRY_TYPE_FILE)
                    file_state.setdefault("relative_path", self._relative_path_for_source_file(rule, source_file))
                    file_state.setdefault("stable_seen_count", FILE_STABILITY_SCAN_COUNT)
        self.save_state()

    def discover_files(self, rule: Rule) -> Dict[str, Dict[str, object]]:
        if is_rclone_remote(rule.source_path):
            return self.discover_remote_files(rule.source_path)
        return self.discover_local_files(rule)

    def discover_local_files(self, rule: Rule) -> Dict[str, Dict[str, object]]:
        files = {}
        source_path = rule.source_path
        excluded_dest = None
        if self._is_path_within(rule.dest_path, source_path):
            excluded_dest = self._path_key(rule.dest_path)
            if self._path_key(source_path) == excluded_dest:
                self.log_error(
                    EventType.SCAN,
                    "dest path is the same as source path, skip local scan",
                    rule_id=rule.rule_id,
                    source_path=source_path,
                    dest_path=rule.dest_path,
                )
                return files

        for root, dirs, file_names in os.walk(source_path):
            if excluded_dest:
                dirs[:] = [
                    name for name in dirs
                    if not self._is_path_within(os.path.join(root, name), excluded_dest)
                ]
            for name in dirs:
                full_path = os.path.join(root, name)
                try:
                    dir_stat = os.stat(full_path)
                except OSError as exc:
                    self.log_error(EventType.ERROR, "failed to stat directory", directory=full_path, error=str(exc))
                    continue
                relative_path = os.path.relpath(full_path, source_path)
                files[full_path] = {
                    "relative_path": relative_path,
                    "size": 0,
                    "mtime_ns": dir_stat.st_mtime_ns,
                    "last_seen": self.now_iso(),
                    "entry_type": ENTRY_TYPE_DIRECTORY,
                }
            for name in file_names:
                full_path = os.path.join(root, name)
                try:
                    stat = os.stat(full_path)
                except OSError as exc:
                    self.log_error(EventType.ERROR, "failed to stat file", file=full_path, error=str(exc))
                    continue
                relative_path = os.path.relpath(full_path, source_path)
                files[full_path] = {
                    "relative_path": relative_path,
                    "size": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                    "last_seen": self.now_iso(),
                    "entry_type": ENTRY_TYPE_FILE,
                }
                self._add_file_to_local_directory_metadata(files, source_path, relative_path, stat.st_size, stat.st_mtime_ns)
        return files

    def discover_remote_files(self, source_path: str) -> Dict[str, Dict[str, object]]:
        command = [
            self.config["rclone_command"],
            "lsjson",
            source_path,
            "--recursive",
            "--no-mimetype",
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=300, check=False)
        if result.returncode != 0:
            error_text = (result.stderr or result.stdout or "").strip() or f"rclone lsjson failed with code {result.returncode}"
            raise RuntimeError(error_text)

        try:
            entries = json.loads(result.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"invalid rclone lsjson output: {exc}") from exc

        files = {}
        now = self.now_iso()
        for item in entries:
            if not isinstance(item, dict):
                continue
            raw_relative_path = item.get("Path")
            if raw_relative_path in (None, ""):
                continue
            relative_path = str(raw_relative_path)
            if not relative_path:
                continue
            full_path = self._join_remote_path(source_path, relative_path)
            entry_type = ENTRY_TYPE_DIRECTORY if item.get("IsDir") else ENTRY_TYPE_FILE
            size = 0 if entry_type == ENTRY_TYPE_DIRECTORY else int(item.get("Size") or 0)
            mtime_ns = self._to_mtime_ns(item.get("ModTime"))
            if entry_type == ENTRY_TYPE_DIRECTORY and full_path in files:
                existing = files[full_path]
                existing["last_seen"] = now
                existing["mtime_ns"] = max(int(existing.get("mtime_ns", 0)), mtime_ns)
                continue
            files[full_path] = {
                "relative_path": relative_path,
                "size": size,
                "mtime_ns": mtime_ns,
                "last_seen": now,
                "entry_type": entry_type,
            }
            if entry_type == ENTRY_TYPE_FILE:
                self._add_file_to_remote_directory_metadata(files, source_path, relative_path, size, mtime_ns, now)
        return files

    def _add_file_to_local_directory_metadata(
        self,
        entries: Dict[str, Dict[str, object]],
        source_root: str,
        relative_path: str,
        size: int,
        mtime_ns: int,
    ) -> None:
        for parent_relative_path in self._relative_parent_paths(relative_path):
            directory_path = os.path.join(source_root, *parent_relative_path.split("/"))
            directory_entry = entries.get(directory_path)
            if not isinstance(directory_entry, dict):
                continue
            directory_entry["size"] = int(directory_entry.get("size", 0)) + size
            directory_entry["mtime_ns"] = max(int(directory_entry.get("mtime_ns", 0)), mtime_ns)

    def _add_file_to_remote_directory_metadata(
        self,
        entries: Dict[str, Dict[str, object]],
        source_root: str,
        relative_path: str,
        size: int,
        mtime_ns: int,
        now: str,
    ) -> None:
        for parent_relative_path in self._relative_parent_paths(relative_path):
            directory_path = self._join_remote_path(source_root, parent_relative_path)
            directory_entry = entries.setdefault(
                directory_path,
                {
                    "relative_path": parent_relative_path,
                    "size": 0,
                    "mtime_ns": 0,
                    "last_seen": now,
                    "entry_type": ENTRY_TYPE_DIRECTORY,
                },
            )
            directory_entry["size"] = int(directory_entry.get("size", 0)) + size
            directory_entry["mtime_ns"] = max(int(directory_entry.get("mtime_ns", 0)), mtime_ns)

    def _join_remote_path(self, source_root: str, relative_path: str) -> str:
        path = Path(relative_path)
        if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
            raise ValueError(f"invalid relative path: {relative_path}")

        source_root = source_root.rstrip("/")
        if source_root.endswith(":"):
            return f"{source_root}{relative_path}"
        return f"{source_root}/{relative_path}"

    def _to_mtime_ns(self, value: object) -> int:
        if not value:
            return 0
        text = str(value)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            timestamp = datetime.fromisoformat(text).timestamp()
            # Check for overflow - nanoseconds must fit in signed 64-bit int
            MAX_TIMESTAMP = (2**63 - 1) / 1_000_000_000
            if timestamp > MAX_TIMESTAMP or timestamp < 0:
                return 0
            return int(timestamp * 1_000_000_000)
        except (ValueError, OverflowError):
            return 0

    def _build_last_check(
        self,
        kind: str,
        started_at: str,
        started_monotonic: float,
        success: bool,
        source_ready: bool,
        discovered_files: int = 0,
        new_files: int = 0,
        removed_files: int = 0,
        queued_files: int = 0,
        error: Optional[str] = None,
    ) -> Dict[str, object]:
        finished_at = self.now_iso()
        return {
            "kind": kind,
            "started_at": started_at,
            "finished_at": finished_at,
            "success": success,
            "source_ready": source_ready,
            "duration_seconds": round(max(time.time() - started_monotonic, 0.0), 3),
            "discovered_files": discovered_files,
            "new_files": new_files,
            "removed_files": removed_files,
            "queued_files": queued_files,
            "error": error,
        }

    def _set_rule_last_check(self, rule_id: str, last_check: Dict[str, object], save: bool = True) -> None:
        with self.state_lock:
            rule_state = self.state["rules"].get(rule_id)
            if not isinstance(rule_state, dict):
                return
            rule_state["last_check"] = last_check

        if save:
            self.save_state()

    def bootstrap_scan(self) -> None:
        for rule in self.rules:
            if not rule.enabled:
                continue

            check_started_at = self.now_iso()
            check_started = time.time()
            if not self.is_path_ready(rule.source_path):
                self._set_rule_last_check(
                    rule.rule_id,
                    self._build_last_check(
                        kind="bootstrap_scan",
                        started_at=check_started_at,
                        started_monotonic=check_started,
                        success=False,
                        source_ready=False,
                        error="source path is not ready",
                    ),
                )
                self.log_error(
                    EventType.SCAN,
                    "source path is not ready during bootstrap",
                    rule_id=rule.rule_id,
                    source_path=rule.source_path,
                )
                continue

            with self.state_lock:
                rule_state = self.state["rules"][rule.rule_id]
                if rule_state.get("initialized", False):
                    continue

            try:
                discovered = self.discover_files(rule)
            except Exception as exc:
                self._set_rule_last_check(
                    rule.rule_id,
                    self._build_last_check(
                        kind="bootstrap_scan",
                        started_at=check_started_at,
                        started_monotonic=check_started,
                        success=False,
                        source_ready=True,
                        error=str(exc),
                    ),
                )
                self.log_error(
                    EventType.ERROR,
                    "bootstrap scan failed",
                    rule_id=rule.rule_id,
                    source_path=rule.source_path,
                    source_kind=rule.source_kind,
                    error=str(exc),
                )
                continue

            with self.state_lock:
                rule_state = self.state["rules"][rule.rule_id]
                files_state = rule_state["files"]
                for path, meta in discovered.items():
                    files_state[path] = {
                        "size": meta["size"],
                        "mtime_ns": meta["mtime_ns"],
                        "status": "baseline",
                        "retry_count": 0,
                        "last_error": None,
                        "last_attempt": None,
                        "last_seen": meta["last_seen"],
                        "relative_path": str(meta.get("relative_path") or self._relative_path_for_source_file(rule, path)),
                        "stable_seen_count": FILE_STABILITY_SCAN_COUNT,
                        "entry_type": str(meta.get("entry_type") or ENTRY_TYPE_FILE),
                    }
                rule_state["initialized"] = True
                rule_state["initialized_at"] = self.now_iso()
                rule_state["last_check"] = self._build_last_check(
                    kind="bootstrap_scan",
                    started_at=check_started_at,
                    started_monotonic=check_started,
                    success=True,
                    source_ready=True,
                    discovered_files=len(discovered),
                )

            self.save_state()
            self.log_event(
                EventType.SCAN,
                "bootstrap scan completed",
                rule_id=rule.rule_id,
                discovered_files=len(discovered),
            )

    def incremental_scan(self) -> None:
        total_new = 0
        tasks_to_enqueue: List[DownloadTask] = []

        for rule in self.rules:
            if not rule.enabled or self.stop_event.is_set():
                continue

            check_started_at = self.now_iso()
            check_started = time.time()
            if not self.is_path_ready(rule.source_path):
                self._set_rule_last_check(
                    rule.rule_id,
                    self._build_last_check(
                        kind="incremental_scan",
                        started_at=check_started_at,
                        started_monotonic=check_started,
                        success=False,
                        source_ready=False,
                        error="source path is not ready",
                    ),
                )
                self.log_error(
                    EventType.SCAN,
                    "source path not ready, skip scan",
                    rule_id=rule.rule_id,
                    source_path=rule.source_path,
                )
                continue

            try:
                discovered = self.discover_files(rule)
            except Exception as exc:
                self._set_rule_last_check(
                    rule.rule_id,
                    self._build_last_check(
                        kind="incremental_scan",
                        started_at=check_started_at,
                        started_monotonic=check_started,
                        success=False,
                        source_ready=True,
                        error=str(exc),
                    ),
                )
                self.log_error(
                    EventType.ERROR,
                    "incremental scan failed",
                    rule_id=rule.rule_id,
                    source_path=rule.source_path,
                    source_kind=rule.source_kind,
                    error=str(exc),
                )
                continue
            new_files = 0
            queued_files = 0
            state_changed = False
            missing_files: List[str] = []

            with self.state_lock:
                rule_state = self.state["rules"][rule.rule_id]
                files_state = rule_state["files"]

                for source_file, meta in discovered.items():
                    relative_path = str(meta.get("relative_path") or self._relative_path_for_source_file(rule, source_file))
                    entry_type = str(meta.get("entry_type") or ENTRY_TYPE_FILE)
                    current_size = int(meta["size"])
                    current_mtime_ns = int(meta["mtime_ns"])
                    if source_file not in files_state:
                        files_state[source_file] = {
                            "size": current_size,
                            "mtime_ns": current_mtime_ns,
                            "status": "observed",
                            "retry_count": 0,
                            "last_error": None,
                            "last_attempt": None,
                            "last_seen": meta["last_seen"],
                            "relative_path": relative_path,
                            "stable_seen_count": 1,
                            "entry_type": entry_type,
                        }
                        new_files += 1
                        state_changed = True
                        continue

                    file_state = files_state[source_file]
                    file_state["last_seen"] = meta["last_seen"]
                    file_state["relative_path"] = relative_path
                    file_state["entry_type"] = entry_type

                    previous_size = int(file_state.get("size", -1))
                    previous_mtime_ns = int(file_state.get("mtime_ns", -1))
                    changed = previous_size != current_size or previous_mtime_ns != current_mtime_ns

                    if changed:
                        file_state["size"] = current_size
                        file_state["mtime_ns"] = current_mtime_ns
                        file_state["stable_seen_count"] = 1
                        state_changed = True
                        if file_state.get("status") != "baseline":
                            file_state["status"] = "observed"
                            file_state["retry_count"] = 0
                            file_state["last_error"] = None
                            file_state["last_attempt"] = None
                        continue

                    stable_seen_count = min(
                        int(file_state.get("stable_seen_count", 1)) + 1,
                        FILE_STABILITY_SCAN_COUNT,
                    )
                    if stable_seen_count != file_state.get("stable_seen_count"):
                        file_state["stable_seen_count"] = stable_seen_count
                        state_changed = True
                    if file_state.get("status") == "observed" and stable_seen_count >= FILE_STABILITY_SCAN_COUNT:
                        file_state["status"] = "pending"
                        state_changed = True

                missing_files = [f for f in list(files_state) if f not in discovered]
                for f in missing_files:
                    del files_state[f]
                    state_changed = True
                directory_task_relative_paths = self._directory_task_relative_paths(files_state)
                for source_file, file_state in files_state.items():
                    if not isinstance(file_state, dict):
                        continue
                    if file_state.get("status") != "pending":
                        continue
                    relative_path = str(file_state.get("relative_path") or self._relative_path_for_source_file(rule, source_file))
                    entry_type = self._entry_type_for_state(file_state)
                    if (
                        entry_type == ENTRY_TYPE_FILE
                        and self._is_covered_by_directory_task(relative_path, directory_task_relative_paths)
                    ):
                        continue
                    if (
                        entry_type == ENTRY_TYPE_DIRECTORY
                        and self._is_covered_by_other_directory_task(relative_path, directory_task_relative_paths)
                    ):
                        continue
                    queued_files += 1
                    tasks_to_enqueue.append(
                        DownloadTask(
                            rule_id=rule.rule_id,
                            source_file=source_file,
                            dest_path=rule.dest_path,
                            relative_path=relative_path,
                            entry_type=entry_type,
                        )
                    )
                rule_state["last_check"] = self._build_last_check(
                    kind="incremental_scan",
                    started_at=check_started_at,
                    started_monotonic=check_started,
                    success=True,
                    source_ready=True,
                    discovered_files=len(discovered),
                    new_files=new_files,
                    removed_files=len(missing_files),
                    queued_files=queued_files,
                )
                state_changed = True

            if state_changed:
                self.save_state()

            removed_files = len(missing_files) if missing_files else 0
            total_new += new_files
            self.log_event(
                EventType.SCAN,
                "incremental scan completed",
                rule_id=rule.rule_id,
                discovered_files=len(discovered),
                new_files=new_files,
                removed_files=removed_files,
            )

        for task in tasks_to_enqueue:
            self.enqueue_download(task)

        self.log_event(EventType.SCAN, "scan cycle finished", total_new_files=total_new)

    def enqueue_retry_candidates(self) -> None:
        max_retry = self.config["max_retry_count"]
        queued_count = 0

        with self.state_lock:
            for rule in self.rules:
                if not rule.enabled:
                    continue
                rule_state = self.state["rules"][rule.rule_id]
                directory_task_relative_paths = self._directory_task_relative_paths(rule_state["files"])
                for source_file, file_state in rule_state["files"].items():
                    status = file_state.get("status")
                    retry_count = int(file_state.get("retry_count", 0))
                    if status not in ("pending", "failed"):
                        continue
                    if status == "failed" and retry_count >= max_retry:
                        continue
                    relative_path = str(file_state.get("relative_path") or self._relative_path_for_source_file(rule, source_file))
                    entry_type = self._entry_type_for_state(file_state)
                    if (
                        entry_type == ENTRY_TYPE_FILE
                        and self._is_covered_by_directory_task(relative_path, directory_task_relative_paths)
                    ):
                        continue
                    if (
                        entry_type == ENTRY_TYPE_DIRECTORY
                        and self._is_covered_by_other_directory_task(relative_path, directory_task_relative_paths)
                    ):
                        continue
                    task = DownloadTask(
                        rule_id=rule.rule_id,
                        source_file=source_file,
                        dest_path=rule.dest_path,
                        relative_path=relative_path,
                        entry_type=entry_type,
                    )
                    if self.enqueue_download(task):
                        queued_count += 1

        if queued_count > 0:
            self.log_event(EventType.DOWNLOAD, "retry candidates queued", queued=queued_count)

    def enqueue_download(self, task: DownloadTask) -> bool:
        key = self._file_key(task.rule_id, task.source_file)

        with self.queue_lock:
            if key in self.queued_files or key in self.in_progress_files:
                return False
            try:
                self.download_queue.put_nowait(task)
            except queue.Full:
                self.log_error(
                    EventType.ERROR,
                    "download queue full, dropping task",
                    rule_id=task.rule_id,
                    source_file=task.source_file,
                    queue_size=MAX_QUEUE_SIZE,
                )
                return False
            self.queued_files.add(key)

        return True

    def start_workers(self) -> None:
        worker_count = self.config["max_concurrent_downloads"]
        for idx in range(worker_count):
            worker = threading.Thread(target=self.download_worker, name=f"download-worker-{idx}", daemon=True)
            worker.start()
            self.workers.append(worker)

    def download_worker(self) -> None:
        while True:
            if self.stop_event.is_set() and self.download_queue.empty():
                return

            try:
                task = self.download_queue.get(timeout=1)
            except queue.Empty:
                continue

            key = self._file_key(task.rule_id, task.source_file)
            with self.queue_lock:
                self.queued_files.discard(key)
                self.in_progress_files.add(key)
                self.active_downloads += 1

            try:
                self.handle_download(task)
            finally:
                with self.queue_lock:
                    self.in_progress_files.discard(key)
                    self.active_downloads -= 1

                self.download_queue.task_done()

    def _terminate_process(self, process: subprocess.Popen, timeout_seconds: int = 10) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                pass

    def _terminate_active_transfers(self) -> None:
        with self.queue_lock:
            processes = [transfer.process for transfer in self.active_transfers.values()]

        for process in processes:
            try:
                self._terminate_process(process)
            except OSError:
                pass

        if processes:
            self.log_event(EventType.SYSTEM, "active rclone processes terminated", count=len(processes))

    def _allocate_rc_port(self) -> int:
        rc_config = self.config.get("rclone_rc", {})
        if not isinstance(rc_config, dict):
            raise RuntimeError("rclone_rc config is invalid")

        host = str(rc_config.get("host", "127.0.0.1"))
        port_min = int(rc_config.get("port_min", 0))
        port_max = int(rc_config.get("port_max", 0))

        with self.rc_port_lock:
            if port_min == 0 and port_max == 0:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                    sock.bind((host, 0))
                    port = int(sock.getsockname()[1])
                self.reserved_rc_ports.add(port)
                return port

            for port in range(port_min, port_max + 1):
                if port in self.reserved_rc_ports:
                    continue
                if not self._can_bind_rc_port(host, port):
                    continue
                self.reserved_rc_ports.add(port)
                return port

        raise RuntimeError("no available rclone_rc port")

    def _can_bind_rc_port(self, host: str, port: int) -> bool:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.bind((host, port))
            return True
        except OSError:
            return False

    def _release_rc_port(self, port: Optional[int]) -> None:
        if port is None:
            return
        with self.rc_port_lock:
            self.reserved_rc_ports.discard(port)

    def _build_rclone_copy_command(
        self,
        source_file: str,
        dest_target: str,
        rc_addr: str,
        entry_type: str,
    ) -> List[str]:
        command = [self.config["rclone_command"], "copy", source_file, dest_target]
        if entry_type == ENTRY_TYPE_DIRECTORY:
            command.append("--create-empty-src-dirs")

        command.extend([
            "--rc",
            "--rc-addr", rc_addr,
            "--transfers", "2",
            "--multi-thread-streams", "4",
            "--multi-thread-cutoff", "100M",
            "--retries", "10",
            "--low-level-retries", "20",
            "--timeout", "1m",
            "--contimeout", "15s",
            "--stats", "1s",
            "--log-level", "DEBUG",
            "--log-file", "/tmp/rclone-pikpak-debug.log",
        ])
        bandwidth_limit = self.config.get("bandwidth_limit_mbps", 0)
        if bandwidth_limit > 0:
            command.extend(["--bwlimit", f"{bandwidth_limit}M"])
        return command

    def _is_rc_address_in_use_error(self, stdout: str, stderr: str) -> bool:
        text = f"{stdout}\n{stderr}".lower()
        return any(marker in text for marker in RCLONE_RC_ADDRESS_IN_USE_MARKERS)

    def _active_transfer_snapshot(self) -> Tuple[List[Tuple[str, ActiveTransfer]], int, int]:
        with self.queue_lock:
            return list(self.active_transfers.items()), self.download_queue.qsize(), self.active_downloads

    def _fetch_rclone_stats(self, rc_url: str) -> Dict[str, Any]:
        rc_config = self.config.get("rclone_rc", {})
        timeout = 2
        if isinstance(rc_config, dict):
            timeout = int(rc_config.get("request_timeout_seconds", 2))

        url = f"{rc_url.rstrip('/')}/core/stats"
        request = urllib.request.Request(
            url,
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                response_body = response.read()
        except urllib.error.HTTPError as exc:
            return {"error": f"http {exc.code}"}
        except Exception as exc:
            return {"error": str(exc.__class__.__name__)}

        try:
            result = json.loads(response_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {"error": "invalid stats response"}

        if not isinstance(result, dict):
            return {"error": "invalid stats response"}
        return result

    def _build_downloading_view(self) -> str:
        snapshot, queued_count, active_count = self._active_transfer_snapshot()
        transfers = [transfer for _key, transfer in snapshot]
        stats_by_key = self._fetch_active_rclone_stats(snapshot)

        return format_downloading_view(
            transfers,
            stats_by_key,
            int(self.config.get("max_concurrent_downloads", 1)),
            queued_count,
            active_count,
        )

    def _fetch_active_rclone_stats(self, snapshot: List[Tuple[str, ActiveTransfer]]) -> Dict[str, Dict[str, Any]]:
        if not snapshot:
            return {}

        if len(snapshot) == 1:
            key, transfer = snapshot[0]
            return {key: self._fetch_rclone_stats(transfer.rc_url)}

        max_workers = min(len(snapshot), MAX_TELEGRAM_RCLONE_STATS_WORKERS)
        stats_by_key: Dict[str, Dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="rclone-stats") as executor:
            future_to_key = {
                executor.submit(self._fetch_rclone_stats, transfer.rc_url): key
                for key, transfer in snapshot
            }
            for future in as_completed(future_to_key):
                key = future_to_key[future]
                try:
                    stats_by_key[key] = future.result()
                except Exception as exc:
                    stats_by_key[key] = {"error": str(exc.__class__.__name__)}
        return stats_by_key

    def _build_rules_state_view(self) -> str:
        rules_state_summary: Dict[str, Dict[str, object]] = {}
        with self.state_lock:
            rules_state = self.state.get("rules", {})
            if not isinstance(rules_state, dict):
                rules_state = {}

            for rule in self.rules:
                rule_state = rules_state.get(rule.rule_id, {})
                if not isinstance(rule_state, dict):
                    rule_state = {}

                files_state = rule_state.get("files", {})
                counts: Dict[str, int] = {}
                if isinstance(files_state, dict):
                    for file_state in files_state.values():
                        if not isinstance(file_state, dict):
                            continue
                        status = str(file_state.get("status") or "unknown")
                        counts[status] = counts.get(status, 0) + 1

                last_check = rule_state.get("last_check")
                if isinstance(last_check, dict):
                    last_check = dict(last_check)

                rules_state_summary[rule.rule_id] = {
                    "initialized": rule_state.get("initialized"),
                    "last_check": last_check,
                    "status_counts": counts,
                }

        state_snapshot = {"rules": rules_state_summary}
        return format_rules_state_view(self.rules, state_snapshot)

    def _mark_download_pending(self, rule_id: str, source_file: str) -> None:
        with self.state_lock:
            rule_state = self.state["rules"].get(rule_id)
            if not rule_state:
                return
            file_state = rule_state["files"].get(source_file)
            if not file_state:
                return

            file_state["status"] = "pending"
            file_state["last_error"] = None

        self.save_state()

    def notify_download_completed(
        self,
        rule_id: str,
        source_file: str,
        dest_path: str,
        duration_seconds: float,
        entry_type: str = ENTRY_TYPE_FILE,
    ) -> None:
        telegram = self.config.get("telegram", {})
        if not isinstance(telegram, dict) or not telegram.get("enabled"):
            return

        message = self._format_download_completed_message(rule_id, source_file, dest_path, duration_seconds, entry_type)
        self._send_telegram_message(message)

    def _format_download_completed_message(
        self,
        rule_id: str,
        source_file: str,
        dest_path: str,
        duration_seconds: float,
        entry_type: str = ENTRY_TYPE_FILE,
    ) -> str:
        filename = Path(source_file.rstrip("/")).name or source_file
        duration_text = self._format_duration(duration_seconds)
        completed_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
        label = "文件夹" if entry_type == ENTRY_TYPE_DIRECTORY else "文件"

        return "\n".join(
            [
                f"✅ {label}同步完成",
                f"{label}: {filename}",
                f"来源: {source_file}",
                f"目标: {dest_path}",
                f"规则: {rule_id}",
                f"耗时: {duration_text}",
                f"完成时间: {completed_at}",
            ]
        )

    def _format_duration(self, seconds: float) -> str:
        total_seconds = max(int(round(seconds)), 0)
        hours, remainder = divmod(total_seconds, 3600)
        minutes, secs = divmod(remainder, 60)
        parts = []
        if hours:
            parts.append(f"{hours}h")
        if minutes or hours:
            parts.append(f"{minutes}m")
        parts.append(f"{secs}s")
        return " ".join(parts)

    def start_telegram_bot(self) -> None:
        telegram = self.config.get("telegram", {})
        if not isinstance(telegram, dict) or not telegram.get("enabled"):
            return

        self.telegram_thread = threading.Thread(target=self._telegram_poll_loop, name="telegram-bot", daemon=True)
        self.telegram_thread.start()
        self.log_event(EventType.NOTIFICATION, "telegram bot polling started")

    def _telegram_poll_loop(self) -> None:
        while not self.stop_event.is_set():
            telegram = self.config.get("telegram", {})
            if not isinstance(telegram, dict):
                return
            poll_timeout = int(telegram.get("poll_timeout_seconds", 25))
            try:
                updates = self._telegram_get_updates(poll_timeout)
            except Exception as exc:
                self.log_error(
                    EventType.ERROR,
                    "telegram polling failed",
                    error=str(exc.__class__.__name__),
                )
                self.stop_event.wait(min(5, poll_timeout))
                continue

            for update in updates:
                if not isinstance(update, dict):
                    continue
                update_id = update.get("update_id")
                try:
                    self._handle_telegram_update(update)
                except Exception as exc:
                    self.log_error(
                        EventType.ERROR,
                        "telegram update handling failed",
                        update_id=update_id,
                        error=str(exc.__class__.__name__),
                    )
                finally:
                    if isinstance(update_id, int):
                        self.telegram_update_offset = max(self.telegram_update_offset or 0, update_id + 1)

            if not updates:
                continue

    def _telegram_get_updates(self, poll_timeout: int) -> List[Dict[str, object]]:
        payload: Dict[str, object] = {
            "timeout": poll_timeout,
            "allowed_updates": ["message", "callback_query"],
        }
        if self.telegram_update_offset is not None:
            payload["offset"] = self.telegram_update_offset

        result = self._telegram_api_request("getUpdates", payload, timeout=poll_timeout + 5)
        if not result:
            self.stop_event.wait(min(5, poll_timeout))
            return []

        updates = result.get("result", [])
        if not isinstance(updates, list):
            return []
        if updates:
            self.log_event(EventType.NOTIFICATION, "telegram updates received", count=len(updates))
        return [update for update in updates if isinstance(update, dict)]

    def _handle_telegram_update(self, update: Dict[str, object]) -> None:
        message = update.get("message")
        if isinstance(message, dict):
            self._handle_telegram_message(message)
            return

        callback_query = update.get("callback_query")
        if isinstance(callback_query, dict):
            self._handle_telegram_callback(callback_query)

    def _telegram_chat_allowed(self, chat: object) -> bool:
        telegram = self.config.get("telegram", {})
        if not isinstance(telegram, dict):
            return False

        chat_id = str(telegram.get("chat_id", "")).strip()
        if not isinstance(chat, dict):
            return False
        received_chat_id = str(chat.get("id", "")).strip()
        return bool(chat_id and received_chat_id == chat_id)

    def _handle_telegram_message(self, message: Dict[str, object]) -> None:
        chat = message.get("chat")
        if not self._telegram_chat_allowed(chat):
            self.log_event(
                EventType.NOTIFICATION,
                "telegram message ignored because chat_id is not allowed",
                received_chat_id=self._telegram_chat_id_for_log(chat),
            )
            return

        text = str(message.get("text") or "").strip()
        if not text:
            return
        command = text.split()[0].split("@", 1)[0]
        if command not in ("/start", "/status", "/menu"):
            return

        chat_id = self._telegram_chat_id_for_log(chat)
        self._send_telegram_menu(
            chat_id=chat_id,
            message_thread_id=self._telegram_message_thread_id_for_reply(message),
            use_config_thread=False,
        )

    def _handle_telegram_callback(self, callback_query: Dict[str, object]) -> None:
        callback_id = str(callback_query.get("id") or "").strip()
        if callback_id:
            self._answer_telegram_callback(callback_id)

        message = callback_query.get("message")
        if not isinstance(message, dict):
            return
        chat = message.get("chat")
        if not self._telegram_chat_allowed(chat):
            self.log_event(
                EventType.NOTIFICATION,
                "telegram callback ignored because chat_id is not allowed",
                received_chat_id=self._telegram_chat_id_for_log(chat),
            )
            return

        chat_id = str(chat.get("id"))
        message_id = message.get("message_id")
        if not isinstance(message_id, int):
            return

        data = str(callback_query.get("data") or "")
        if data == "downloading":
            live_session = self._open_telegram_live_progress_session(chat_id, message_id)
            text = self._build_downloading_view()
            if live_session is None:
                self._edit_telegram_message(chat_id, message_id, text, self._telegram_menu_markup())
                return

            session_key, session_id = live_session
            self._edit_telegram_live_progress_message(
                session_key,
                session_id,
                chat_id,
                message_id,
                text,
                self._telegram_menu_markup(),
            )
            self._start_telegram_live_progress(chat_id, message_id, session_key, session_id, text)
            return

        if data == "states":
            self._cancel_telegram_live_progress(chat_id, message_id)
            text = self._build_rules_state_view()
            self._edit_telegram_message(
                chat_id,
                message_id,
                text,
                self._telegram_menu_markup(),
                parse_mode=TELEGRAM_MARKDOWN_PARSE_MODE,
            )

    def _telegram_menu_markup(self) -> Dict[str, object]:
        return {
            "inline_keyboard": [
                [
                    {"text": "Downloading", "callback_data": "downloading"},
                    {"text": "States", "callback_data": "states"},
                ]
            ]
        }

    def _telegram_chat_id_for_log(self, chat: object) -> str:
        if not isinstance(chat, dict):
            return ""
        return str(chat.get("id", "")).strip()

    def _telegram_message_thread_id_for_reply(self, message: Dict[str, object]) -> Optional[int]:
        message_thread_id = message.get("message_thread_id")
        if isinstance(message_thread_id, int) and message_thread_id > 0:
            return message_thread_id
        return None

    def _send_telegram_menu(
        self,
        chat_id: Optional[str] = None,
        message_thread_id: Optional[int] = None,
        use_config_thread: bool = True,
    ) -> Optional[Dict[str, object]]:
        active_downloads, queued_downloads = self.get_download_counters()
        text = "\n".join(
            [
                "Sync daemon",
                f"Active: {active_downloads} | Queued: {queued_downloads}",
                f"Updated: {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}",
            ]
        )
        return self._send_telegram_message(
            text,
            reply_markup=self._telegram_menu_markup(),
            chat_id=chat_id,
            message_thread_id=message_thread_id,
            use_config_thread=use_config_thread,
        )

    def _open_telegram_live_progress_session(
        self,
        chat_id: str,
        message_id: int,
    ) -> Optional[Tuple[Tuple[str, int], int]]:
        telegram = self.config.get("telegram", {})
        if not isinstance(telegram, dict):
            return None
        live_seconds = int(telegram.get("progress_live_seconds", 120))
        if live_seconds <= 0:
            self._cancel_telegram_live_progress(chat_id, message_id)
            return None

        session_key = (chat_id, message_id)
        with self.telegram_live_progress_lock:
            session_id = self.telegram_live_progress_sessions.get(session_key, 0) + 1
            self.telegram_live_progress_sessions[session_key] = session_id
        return session_key, session_id

    def _cancel_telegram_live_progress(self, chat_id: str, message_id: int) -> None:
        with self.telegram_live_progress_lock:
            self.telegram_live_progress_sessions.pop((chat_id, message_id), None)

    def _telegram_live_progress_is_current(self, session_key: Tuple[str, int], session_id: int) -> bool:
        with self.telegram_live_progress_lock:
            return self.telegram_live_progress_sessions.get(session_key) == session_id

    def _clear_telegram_live_progress_session(self, session_key: Tuple[str, int], session_id: int) -> None:
        with self.telegram_live_progress_lock:
            if self.telegram_live_progress_sessions.get(session_key) == session_id:
                self.telegram_live_progress_sessions.pop(session_key, None)

    def _start_telegram_live_progress(
        self,
        chat_id: str,
        message_id: int,
        session_key: Tuple[str, int],
        session_id: int,
        initial_text: str,
    ) -> None:
        thread = threading.Thread(
            target=self._telegram_live_progress_loop,
            args=(chat_id, message_id, session_key, session_id, initial_text),
            name="telegram-live-progress",
            daemon=True,
        )
        thread.start()

    def _telegram_live_progress_loop(
        self,
        chat_id: str,
        message_id: int,
        session_key: Tuple[str, int],
        session_id: int,
        initial_text: str,
    ) -> None:
        last_text = initial_text
        idle_refresh_failures = 0
        next_refresh_at = time.monotonic() + TELEGRAM_LIVE_PROGRESS_INTERVAL_SECONDS
        try:
            while self._telegram_live_progress_is_current(session_key, session_id):
                wait_seconds = max(0.0, next_refresh_at - time.monotonic())
                if self.stop_event.wait(wait_seconds):
                    return
                next_refresh_at += TELEGRAM_LIVE_PROGRESS_INTERVAL_SECONDS

                if not self._telegram_live_progress_is_current(session_key, session_id):
                    return

                text = self._build_downloading_view()
                if not self._telegram_live_progress_is_current(session_key, session_id):
                    return

                text_sent = text == last_text
                if text != last_text:
                    result = self._edit_telegram_live_progress_message(
                        session_key,
                        session_id,
                        chat_id,
                        message_id,
                        text,
                        self._telegram_menu_markup(),
                    )
                    if result is not None:
                        last_text = text
                        text_sent = True

                active_downloads, queued_downloads = self.get_download_counters()
                if active_downloads == 0 and queued_downloads == 0:
                    idle_text = self._build_downloading_view()
                    if idle_text != text:
                        text = idle_text
                        text_sent = text == last_text
                        if text != last_text:
                            result = self._edit_telegram_live_progress_message(
                                session_key,
                                session_id,
                                chat_id,
                                message_id,
                                text,
                                self._telegram_menu_markup(),
                            )
                            if result is not None:
                                last_text = text
                                text_sent = True

                    if text_sent:
                        return
                    idle_refresh_failures += 1
                    if idle_refresh_failures >= 3:
                        return
                else:
                    idle_refresh_failures = 0

                if next_refresh_at < time.monotonic():
                    next_refresh_at = time.monotonic()
        finally:
            self._clear_telegram_live_progress_session(session_key, session_id)

    def _send_telegram_message(
        self,
        message: str,
        reply_markup: Optional[Dict[str, object]] = None,
        chat_id: Optional[str] = None,
        message_thread_id: Optional[int] = None,
        use_config_thread: bool = True,
        parse_mode: Optional[str] = None,
    ) -> Optional[Dict[str, object]]:
        telegram = self.config.get("telegram", {})
        if not isinstance(telegram, dict):
            return None

        target_chat_id = chat_id if chat_id is not None else str(telegram.get("chat_id", "")).strip()
        target_chat_id = str(target_chat_id).strip()
        if not target_chat_id:
            return None

        payload: Dict[str, object] = {
            "chat_id": target_chat_id,
            "text": telegram_safe_truncate(message),
            "disable_web_page_preview": True,
        }
        target_message_thread_id: Optional[int] = message_thread_id
        if use_config_thread and target_message_thread_id is None:
            configured_message_thread_id = telegram.get("message_thread_id")
            if isinstance(configured_message_thread_id, int):
                target_message_thread_id = configured_message_thread_id

        if target_message_thread_id is not None:
            payload["message_thread_id"] = target_message_thread_id
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        if parse_mode is not None:
            payload["parse_mode"] = parse_mode

        result = self._telegram_api_request("sendMessage", payload, timeout=10)
        if result:
            self.log_event(EventType.NOTIFICATION, "telegram notification sent", chat_id=target_chat_id)
        return result

    def _edit_telegram_message(
        self,
        chat_id: str,
        message_id: int,
        message: str,
        reply_markup: Optional[Dict[str, object]] = None,
        parse_mode: Optional[str] = None,
    ) -> Optional[Dict[str, object]]:
        with self.telegram_edit_lock:
            return self._edit_telegram_message_unlocked(chat_id, message_id, message, reply_markup, parse_mode)

    def _edit_telegram_live_progress_message(
        self,
        session_key: Tuple[str, int],
        session_id: int,
        chat_id: str,
        message_id: int,
        message: str,
        reply_markup: Optional[Dict[str, object]] = None,
    ) -> Optional[Dict[str, object]]:
        with self.telegram_edit_lock:
            if not self._telegram_live_progress_is_current(session_key, session_id):
                return None
            return self._edit_telegram_message_unlocked(chat_id, message_id, message, reply_markup)

    def _edit_telegram_message_unlocked(
        self,
        chat_id: str,
        message_id: int,
        message: str,
        reply_markup: Optional[Dict[str, object]] = None,
        parse_mode: Optional[str] = None,
    ) -> Optional[Dict[str, object]]:
        payload: Dict[str, object] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": telegram_safe_truncate(message),
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        if parse_mode is not None:
            payload["parse_mode"] = parse_mode
        return self._telegram_api_request("editMessageText", payload, timeout=10)

    def _answer_telegram_callback(self, callback_id: str) -> Optional[Dict[str, object]]:
        return self._telegram_api_request(
            "answerCallbackQuery",
            {"callback_query_id": callback_id},
            timeout=3,
        )

    def _telegram_api_request(
        self,
        method: str,
        payload: Dict[str, object],
        timeout: int,
    ) -> Optional[Dict[str, object]]:
        telegram = self.config.get("telegram", {})
        if not isinstance(telegram, dict):
            return None

        bot_token = str(telegram.get("bot_token", "")).strip()
        if not bot_token:
            return None

        data = json.dumps(payload).encode("utf-8")
        url = f"https://api.telegram.org/bot{bot_token}/{method}"
        request = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                response_body = response.read()
        except urllib.error.HTTPError as exc:
            self._log_telegram_http_error(method, exc)
            return None
        except Exception as exc:
            if method == "getUpdates" and exc.__class__.__name__ == "TimeoutError":
                return None
            self.log_error(
                EventType.ERROR,
                "telegram api request failed",
                method=method,
                error=str(exc.__class__.__name__),
            )
            return None

        try:
            result = json.loads(response_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.log_error(EventType.ERROR, "telegram api request failed", method=method, error="invalid response")
            return None

        if not result.get("ok"):
            description = str(result.get("description", "telegram api error"))
            self.log_error(EventType.ERROR, "telegram api request failed", method=method, error=description)
            return None

        return result

    def _log_telegram_http_error(self, method: str, exc: urllib.error.HTTPError) -> None:
        error_text = f"http {exc.code}"
        try:
            body = exc.read().decode("utf-8")
            result = json.loads(body)
            description = result.get("description")
            if description:
                error_text = f"{error_text}: {description}"
        except Exception:
            pass

        self.log_error(EventType.ERROR, "telegram api request failed", method=method, error=error_text)

    def _mark_download_completed(
        self,
        rule_id: str,
        source_file: str,
        dest_path: str,
        start_time: float,
        entry_type: str = ENTRY_TYPE_FILE,
    ) -> None:
        self.update_download_state(rule_id, source_file, success=True, error=None)
        duration = round(time.time() - start_time, 3)
        self.log_event(
            EventType.DOWNLOAD,
            "download completed",
            rule_id=rule_id,
            source_file=source_file,
            entry_type=entry_type,
            duration_seconds=duration,
        )
        self.notify_download_completed(rule_id, source_file, dest_path, duration, entry_type)

    def _mark_descendants_synced(self, rule_state: Dict[str, object], directory_relative_path: str) -> None:
        files_state = rule_state.get("files", {})
        if not isinstance(files_state, dict):
            return

        changed = False
        for source_file, file_state in files_state.items():
            if not isinstance(file_state, dict):
                continue
            if self._entry_type_for_state(file_state) != ENTRY_TYPE_FILE:
                continue
            relative_path = str(file_state.get("relative_path") or "")
            if not self._is_relative_path_under_directory(relative_path, directory_relative_path):
                continue
            file_state["status"] = "synced"
            file_state["retry_count"] = 0
            file_state["last_error"] = None
            file_state["stable_seen_count"] = FILE_STABILITY_SCAN_COUNT
            file_state["last_attempt"] = self.now_iso()
            changed = True

        if changed:
            self.save_state()

    def handle_download(self, task: DownloadTask) -> None:
        rule_id = task.rule_id
        source_file = task.source_file
        dest_path = task.dest_path
        entry_type = task.entry_type
        if self.stop_event.is_set():
            return

        try:
            dest_file = self._dest_file_target(dest_path, task.relative_path)
            dest_target = dest_file
            if entry_type != ENTRY_TYPE_DIRECTORY:
                dest_target = self._dest_parent_target(dest_path, task.relative_path)
            if not is_rclone_remote(dest_path):
                if entry_type == ENTRY_TYPE_DIRECTORY:
                    Path(dest_file).mkdir(parents=True, exist_ok=True)
                else:
                    Path(dest_file).parent.mkdir(parents=True, exist_ok=True)
        except (OSError, ValueError) as exc:
            error_text = f"failed to prepare destination: {exc}"
            self.update_download_state(rule_id, source_file, success=False, error=error_text)
            self.log_error(EventType.ERROR, "download failed", rule_id=rule_id, source_file=source_file, error=error_text)
            return

        # Sync state for retry tasks: once picked up and running again, it should no longer stay in "failed".
        self._mark_download_pending(rule_id, source_file)

        start_time = time.time()
        self.log_event(
            EventType.DOWNLOAD,
            "download started",
            rule_id=rule_id,
            source_file=source_file,
            dest_file=str(dest_file),
        )

        key = self._file_key(rule_id, source_file)
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            rc_port: Optional[int] = None
            process: Optional[subprocess.Popen] = None
            try:
                rc_port = self._allocate_rc_port()
                rc_config = self.config.get("rclone_rc", {})
                host = "127.0.0.1"
                if isinstance(rc_config, dict):
                    host = str(rc_config.get("host", "127.0.0.1"))
                rc_addr = f"{host}:{rc_port}"
                rc_url = f"http://{rc_addr}"
                command = self._build_rclone_copy_command(source_file, dest_target, rc_addr, entry_type)

                process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True
                )

                with self.queue_lock:
                    self.active_transfers[key] = ActiveTransfer(
                        rule_id=rule_id,
                        source_file=source_file,
                        dest_file=str(dest_file),
                        started_at=start_time,
                        process=process,
                        rc_url=rc_url,
                    )

                try:
                    download_timeout = self.config.get("download_timeout_seconds", 0)
                    timeout = int(download_timeout) if download_timeout else None
                    stdout, stderr = process.communicate(timeout=timeout)
                    returncode = process.returncode
                except subprocess.TimeoutExpired:
                    self._terminate_process(process)
                    stdout, stderr = process.communicate()
                    if self.stop_event.is_set():
                        self._mark_download_pending(rule_id, source_file)
                        self.log_event(
                            EventType.DOWNLOAD,
                            "download interrupted by shutdown",
                            rule_id=rule_id,
                            source_file=source_file,
                        )
                        return
                    self.update_download_state(rule_id, source_file, success=False, error="rclone timeout")
                    self.log_error(EventType.ERROR, "download timeout", rule_id=rule_id, source_file=source_file)
                    return
                finally:
                    with self.queue_lock:
                        self.active_transfers.pop(key, None)

                if returncode == 0:
                    self._mark_download_completed(rule_id, source_file, str(dest_file), start_time, entry_type)
                    return

                if self.stop_event.is_set():
                    self._mark_download_pending(rule_id, source_file)
                    self.log_event(
                        EventType.DOWNLOAD,
                        "download interrupted by shutdown",
                        rule_id=rule_id,
                        source_file=source_file,
                        returncode=returncode,
                    )
                    return

                stderr_text = (stderr or "").strip()
                stdout_text = (stdout or "").strip()
                error_text = stderr_text if stderr_text else stdout_text
                if attempt < max_attempts and self._is_rc_address_in_use_error(stdout_text, stderr_text):
                    self.log_event(
                        EventType.DOWNLOAD,
                        "rclone rc port collision, retrying",
                        rule_id=rule_id,
                        source_file=source_file,
                        attempt=attempt,
                    )
                    continue

                self.update_download_state(rule_id, source_file, success=False, error=error_text)
                self.log_error(
                    EventType.ERROR,
                    "download failed",
                    rule_id=rule_id,
                    source_file=source_file,
                    returncode=returncode,
                    error=error_text,
                )
                return

            except OSError as exc:
                error_text = str(exc)
                self.update_download_state(rule_id, source_file, success=False, error=error_text)
                self.log_error(
                    EventType.ERROR,
                    "download process error",
                    rule_id=rule_id,
                    source_file=source_file,
                    error=error_text,
                )
                return
            except RuntimeError as exc:
                error_text = str(exc)
                self.update_download_state(rule_id, source_file, success=False, error=error_text)
                self.log_error(
                    EventType.ERROR,
                    "download failed",
                    rule_id=rule_id,
                    source_file=source_file,
                    error=error_text,
                )
                return
            finally:
                if process is not None:
                    with self.queue_lock:
                        self.active_transfers.pop(key, None)
                self._release_rc_port(rc_port)

    def update_download_state(self, rule_id: str, source_file: str, success: bool, error: Optional[str]) -> None:
        entry_type = ENTRY_TYPE_FILE
        relative_path = ""
        with self.state_lock:
            rule_state = self.state["rules"].get(rule_id)
            if not rule_state:
                return
            file_state = rule_state["files"].get(source_file)
            if not file_state:
                return
            entry_type = self._entry_type_for_state(file_state)
            relative_path = str(file_state.get("relative_path") or "")
            if not relative_path:
                rule = next((item for item in self.rules if item.rule_id == rule_id), None)
                if rule is not None:
                    relative_path = self._relative_path_for_source_file(rule, source_file)

            file_state["last_attempt"] = self.now_iso()
            if success:
                file_state["status"] = "synced"
                file_state["last_error"] = None
                file_state["retry_count"] = 0
                file_state["stable_seen_count"] = FILE_STABILITY_SCAN_COUNT
            else:
                file_state["retry_count"] = int(file_state.get("retry_count", 0)) + 1
                max_retry = self.config["max_retry_count"]
                if file_state["retry_count"] >= max_retry:
                    file_state["status"] = "permanent_failed"
                else:
                    file_state["status"] = "failed"
                file_state["last_error"] = error

        self.save_state()

        if success and entry_type == ENTRY_TYPE_DIRECTORY and relative_path:
            with self.state_lock:
                rule_state = self.state["rules"].get(rule_id)
                if isinstance(rule_state, dict):
                    self._mark_descendants_synced(rule_state, relative_path)

    def refresh_mount(self) -> None:
        if self.stop_event.is_set():
            return

        service_name = self.config["rclone_service_name"]
        if not service_name:
            self.log_event(EventType.REFRESH, "refresh skipped because no rclone service is configured")
            return
        active_downloads, queued_downloads = self.get_download_counters()
        if active_downloads > 0 or queued_downloads > 0:
            self.log_event(
                EventType.REFRESH,
                "refresh skipped because downloads are active or queued",
                service_name=service_name,
                active_downloads=active_downloads,
                queued_downloads=queued_downloads,
            )
            return

        self.pause_event.set()
        self.log_event(EventType.REFRESH, "refresh started", service_name=service_name)

        try:
            active_downloads, queued_downloads = self.get_download_counters()
            if active_downloads > 0 or queued_downloads > 0:
                self.log_event(
                    EventType.REFRESH,
                    "refresh aborted because downloads were queued after pause",
                    service_name=service_name,
                    active_downloads=active_downloads,
                    queued_downloads=queued_downloads,
                )
                return

            try:
                result = subprocess.run(
                    ["systemctl", "restart", service_name],
                    capture_output=True,
                    text=True,
                    timeout=120,
                    check=False,
                )
                if result.returncode != 0:
                    self.log_error(
                        EventType.ERROR,
                        "refresh command failed",
                        service_name=service_name,
                        returncode=result.returncode,
                        error=(result.stderr or result.stdout or "").strip(),
                    )
                else:
                    self.log_event(EventType.REFRESH, "service restarted", service_name=service_name)
            except subprocess.TimeoutExpired:
                self.log_error(EventType.ERROR, "refresh timeout", service_name=service_name)
            except OSError as exc:
                self.log_error(EventType.ERROR, "refresh process error", service_name=service_name, error=str(exc))

            ready = self.wait_for_mount_ready(total_wait_seconds=120, probe_interval_seconds=5)
            if ready:
                self.log_event(EventType.REFRESH, "mount ready after refresh")
            else:
                self.log_error(EventType.ERROR, "mount not ready after refresh timeout")
        finally:
            self.pause_event.clear()

    def wait_for_mount_ready(self, total_wait_seconds: int, probe_interval_seconds: int) -> bool:
        deadline = time.time() + total_wait_seconds

        while time.time() < deadline and not self.stop_event.is_set():
            all_ready = True
            for rule in self.rules:
                if not rule.enabled:
                    continue
                if not self.is_path_ready(rule.source_path):
                    all_ready = False
                    self.log_event(
                        EventType.REFRESH,
                        "mount probe failed, retrying",
                        rule_id=rule.rule_id,
                        source_path=rule.source_path,
                    )
                    break

            if all_ready:
                return True

            time.sleep(probe_interval_seconds)

        return False

    def is_path_ready(self, path: str) -> bool:
        if is_rclone_remote(path):
            return self.is_remote_ready(path)
        if not os.path.isdir(path):
            return False

        # Use external command with timeout to avoid potential blocking on stale mounts.
        try:
            result = subprocess.run(
                ["ls", "-1", path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
            return result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def is_remote_ready(self, path: str) -> bool:
        try:
            result = subprocess.run(
                [self.config["rclone_command"], "lsf", path, "--max-depth", "1"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
            )
            return result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def wait_for_download_idle(self, timeout_seconds: int) -> None:
        deadline = time.time() + timeout_seconds

        while time.time() < deadline and not self.stop_event.is_set():
            with self.queue_lock:
                active = self.active_downloads
                queued = self.download_queue.qsize()
            if active == 0 and queued == 0:
                return
            time.sleep(1)

        with self.queue_lock:
            active = self.active_downloads
            queued = self.download_queue.qsize()
        self.log_event(EventType.REFRESH, "download idle wait ended", active_downloads=active, queued_downloads=queued)

    def _drain_pending_queue(self) -> None:
        drained = 0
        while True:
            try:
                task = self.download_queue.get_nowait()
                key = self._file_key(task.rule_id, task.source_file)
                with self.queue_lock:
                    self.queued_files.discard(key)
                self.download_queue.task_done()
                drained += 1
            except queue.Empty:
                break

        if drained > 0:
            self.log_event(EventType.SYSTEM, "pending queue drained", dropped_tasks=drained)

    def shutdown(self) -> None:
        self.stop_event.set()
        self._systemd_notify("STOPPING=1", "STATUS=shutting down")
        self._drain_pending_queue()
        self._terminate_active_transfers()
        self.wait_for_download_idle(timeout_seconds=300)

        for worker in self.workers:
            worker.join(timeout=2)
        if self.watchdog_thread is not None:
            self.watchdog_thread.join(timeout=2)
        if self.telegram_thread is not None:
            self.telegram_thread.join(timeout=2)

        self.save_state()
        self.log_event(EventType.SYSTEM, "daemon stopped")

    @staticmethod
    def now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _file_key(rule_id: str, source_file: str) -> str:
        return f"{rule_id}:{source_file}"


def main() -> int:
    base_dir = Path(__file__).resolve().parent
    daemon = SyncDaemon(base_dir)
    return daemon.run()


if __name__ == "__main__":
    raise SystemExit(main())
