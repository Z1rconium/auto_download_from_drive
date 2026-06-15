#!/usr/bin/env python3
import json
import logging
import os
import queue
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union
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

# Configuration limits
MAX_SCAN_INTERVAL_SECONDS = 86400
MAX_REFRESH_INTERVAL_SECONDS = 86400
MAX_CONCURRENT_DOWNLOADS = 100
MAX_RETRY_COUNT = 100
MAX_DOWNLOAD_TIMEOUT_SECONDS = 86400
MAX_BANDWIDTH_LIMIT_MBPS = 10000
MAX_QUEUE_SIZE = 10000

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
        "message_thread_id": None
    },
    "rules": [
        {
            "source_path": "/path/to/mounted/rclone/folder",
            "dest_path": "/path/to/local/download/folder",
            "enabled": False,
            "_comment": "source_path supports local mount paths like /mnt/pikpak/My Pack and rclone remotes like pikpak:My Pack. Set enabled=true after paths are valid."
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
        self.active_processes: Dict[str, subprocess.Popen] = {}
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

    def _dest_file_path(self, dest_root: str, relative_path: str) -> Path:
        path = Path(relative_path)

        # Check for absolute path
        if path.is_absolute():
            raise ValueError(f"relative path must not be absolute: {relative_path}")

        # Check each path component for invalid entries
        for part in path.parts:
            if part in ("", ".", ".."):
                raise ValueError(f"invalid path component: {part}")
            # Check for colon in path component (Windows drive letters, UNC paths)
            if ':' in part:
                raise ValueError(f"invalid path component (contains colon): {part}")

        dest_root_path = Path(dest_root).resolve()
        dest_file = dest_root_path.joinpath(*path.parts)

        # Resolve symlinks and verify the path stays within dest_root
        try:
            dest_file_resolved = dest_file.resolve()
            dest_file_resolved.relative_to(dest_root_path)
        except (ValueError, RuntimeError):
            raise ValueError(f"destination path escapes root: {relative_path}")

        return dest_file

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
            return True

        with self.config_path.open("w", encoding="utf-8") as fp:
            json.dump(DEFAULT_CONFIG, fp, indent=2)
            fp.write("\n")

        print(
            f"{CONFIG_FILE} created at {self.config_path}. "
            "Please edit it and restart the daemon.",
            file=sys.stderr,
        )
        return False

    def load_config(self) -> None:
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
        message_thread_id_raw = telegram_cfg.get("message_thread_id", None)
        message_thread_id = None

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
        }

    def load_state(self) -> None:
        if not self.state_path.exists():
            self.save_state()
            return

        try:
            with self.state_path.open("r", encoding="utf-8") as fp:
                self.state = json.load(fp)
        except Exception as exc:
            backup_path = self.state_path.with_suffix(".json.bak")
            self.log_error(EventType.ERROR, "failed to load state, trying backup", error=str(exc))
            if backup_path.exists():
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
                except OSError as exc:
                    self.log_error(EventType.ERROR, "failed to create state backup", error=str(exc))

            with tmp_path.open("w", encoding="utf-8") as fp:
                json.dump(self.state, fp, indent=2)
                fp.write("\n")
            os.replace(tmp_path, self.state_path)

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
        self.bootstrap_scan()

        self.install_signal_handlers()
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
                            "files": {},
                        }
                    else:
                        existing["dest_path"] = rule.dest_path
                        existing["enabled"] = rule.enabled
                        if not isinstance(existing.get("files"), dict):
                            existing["files"] = {}
                        existing.setdefault("initialized", False)
                for source_file, file_state in rules_state[rule.rule_id]["files"].items():
                    if not isinstance(file_state, dict):
                        continue
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
                }
        return files

    def discover_remote_files(self, source_path: str) -> Dict[str, Dict[str, object]]:
        command = [
            self.config["rclone_command"],
            "lsjson",
            source_path,
            "--recursive",
            "--files-only",
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
            files[full_path] = {
                "relative_path": relative_path,
                "size": int(item.get("Size") or 0),
                "mtime_ns": self._to_mtime_ns(item.get("ModTime")),
                "last_seen": now,
            }
        return files

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

    def bootstrap_scan(self) -> None:
        for rule in self.rules:
            if not rule.enabled:
                continue

            if not self.is_path_ready(rule.source_path):
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
                    }
                rule_state["initialized"] = True
                rule_state["initialized_at"] = self.now_iso()

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

            if not self.is_path_ready(rule.source_path):
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
            state_changed = False

            with self.state_lock:
                rule_state = self.state["rules"][rule.rule_id]
                files_state = rule_state["files"]

                for source_file, meta in discovered.items():
                    relative_path = str(meta.get("relative_path") or self._relative_path_for_source_file(rule, source_file))
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
                        }
                        new_files += 1
                        state_changed = True
                        continue

                    file_state = files_state[source_file]
                    file_state["last_seen"] = meta["last_seen"]
                    file_state["relative_path"] = relative_path

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
                        tasks_to_enqueue.append(
                            DownloadTask(
                                rule_id=rule.rule_id,
                                source_file=source_file,
                                dest_path=rule.dest_path,
                                relative_path=relative_path,
                            )
                        )

                missing_files = [f for f in list(files_state) if f not in discovered]
                for f in missing_files:
                    del files_state[f]
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
                for source_file, file_state in rule_state["files"].items():
                    status = file_state.get("status")
                    retry_count = int(file_state.get("retry_count", 0))
                    if status not in ("pending", "failed"):
                        continue
                    if status == "failed" and retry_count >= max_retry:
                        continue
                    relative_path = str(file_state.get("relative_path") or self._relative_path_for_source_file(rule, source_file))
                    task = DownloadTask(
                        rule_id=rule.rule_id,
                        source_file=source_file,
                        dest_path=rule.dest_path,
                        relative_path=relative_path,
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
        processes = list(self.active_processes.values())

        for process in processes:
            try:
                self._terminate_process(process)
            except OSError:
                pass

        if processes:
            self.log_event(EventType.SYSTEM, "active rclone processes terminated", count=len(processes))

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

    def notify_download_completed(self, rule_id: str, source_file: str, dest_path: str, duration_seconds: float) -> None:
        telegram = self.config.get("telegram", {})
        if not isinstance(telegram, dict) or not telegram.get("enabled"):
            return

        message = self._format_download_completed_message(rule_id, source_file, dest_path, duration_seconds)
        self._send_telegram_message(message)

    def _format_download_completed_message(
        self,
        rule_id: str,
        source_file: str,
        dest_path: str,
        duration_seconds: float,
    ) -> str:
        filename = Path(source_file.rstrip("/")).name or source_file
        duration_text = self._format_duration(duration_seconds)
        completed_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")

        return "\n".join(
            [
                "✅ 文件同步完成",
                f"文件: {filename}",
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

    def _send_telegram_message(self, message: str) -> None:
        telegram = self.config.get("telegram", {})
        if not isinstance(telegram, dict):
            return

        bot_token = str(telegram.get("bot_token", "")).strip()
        chat_id = str(telegram.get("chat_id", "")).strip()
        if not bot_token or not chat_id:
            return

        # Safely truncate UTF-8 message to Telegram's 4096 character limit
        max_length = 4096
        truncated_message = message
        if len(message.encode('utf-8')) > max_length:
            encoded = message.encode('utf-8')[:max_length]
            # Remove incomplete UTF-8 characters at the end
            while encoded:
                try:
                    truncated_message = encoded.decode('utf-8')
                    break
                except UnicodeDecodeError:
                    encoded = encoded[:-1]

        payload = {
            "chat_id": chat_id,
            "text": truncated_message,
            "disable_web_page_preview": "true",
        }
        message_thread_id = telegram.get("message_thread_id")
        if message_thread_id is not None:
            payload["message_thread_id"] = str(message_thread_id)

        data = urllib.parse.urlencode(payload).encode("utf-8")
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        request = urllib.request.Request(url, data=data, method="POST")

        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                response_body = response.read()
        except urllib.error.HTTPError as exc:
            self._log_telegram_http_error(exc)
            return
        except Exception as exc:
            self.log_error(EventType.ERROR, "telegram notification failed", error=str(exc.__class__.__name__))
            return

        try:
            result = json.loads(response_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.log_error(EventType.ERROR, "telegram notification failed", error="invalid response")
            return

        if not result.get("ok"):
            description = str(result.get("description", "telegram api error"))
            self.log_error(EventType.ERROR, "telegram notification failed", error=description)
            return

        self.log_event(EventType.NOTIFICATION, "telegram notification sent", chat_id=chat_id)

    def _log_telegram_http_error(self, exc: urllib.error.HTTPError) -> None:
        error_text = f"http {exc.code}"
        try:
            body = exc.read().decode("utf-8")
            result = json.loads(body)
            description = result.get("description")
            if description:
                error_text = f"{error_text}: {description}"
        except Exception:
            pass

        self.log_error(EventType.ERROR, "telegram notification failed", error=error_text)

    def _mark_download_completed(
        self,
        rule_id: str,
        source_file: str,
        dest_path: str,
        start_time: float,
    ) -> None:
        self.update_download_state(rule_id, source_file, success=True, error=None)
        duration = round(time.time() - start_time, 3)
        self.log_event(
            EventType.DOWNLOAD,
            "download completed",
            rule_id=rule_id,
            source_file=source_file,
            duration_seconds=duration,
        )
        self.notify_download_completed(rule_id, source_file, dest_path, duration)

    def handle_download(self, task: DownloadTask) -> None:
        rule_id = task.rule_id
        source_file = task.source_file
        dest_path = task.dest_path
        if self.stop_event.is_set():
            return

        try:
            dest_file = self._dest_file_path(dest_path, task.relative_path)
            dest_file.parent.mkdir(parents=True, exist_ok=True)
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

        try:
            command = [
                self.config["rclone_command"], "copyto", source_file, str(dest_file),
                "--transfers", "2",
                "--multi-thread-streams", "4",
                "--multi-thread-cutoff", "100M",
                "--retries", "10",
                "--low-level-retries", "20",
                "--timeout", "1m",
                "--contimeout", "15s",
            ]
            bandwidth_limit = self.config.get("bandwidth_limit_mbps", 0)
            if bandwidth_limit > 0:
                command.extend(["--bwlimit", f"{bandwidth_limit}M"])

            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )

            key = self._file_key(rule_id, source_file)
            self.active_processes[key] = process

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
                self.active_processes.pop(key, None)

            if returncode == 0:
                self._mark_download_completed(rule_id, source_file, str(dest_file), start_time)
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

    def update_download_state(self, rule_id: str, source_file: str, success: bool, error: Optional[str]) -> None:
        with self.state_lock:
            rule_state = self.state["rules"].get(rule_id)
            if not rule_state:
                return
            file_state = rule_state["files"].get(source_file)
            if not file_state:
                return

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
