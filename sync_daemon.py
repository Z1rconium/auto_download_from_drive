#!/usr/bin/env python3
import json
import logging
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Dict, List, Optional, Tuple


CONFIG_FILE = "config.json"
STATE_FILE = "sync_state.json"
LOG_FILE = "sync.log"
SERVICE_NAME = "rclone-pikpak"

DEFAULT_CONFIG = {
    "_comment": "Edit this file and restart the daemon.",
    "scan_interval_seconds": 300,
    "rclone_refresh_interval_seconds": 1800,
    "max_concurrent_downloads": 3,
    "max_retry_count": 5,
    "bandwidth_limit_mbps": 0,
    "rclone_command": "rclone",
    "rclone_service_name": SERVICE_NAME,
    "rules": [
        {
            "source_path": "/path/to/mounted/rclone/folder",
            "dest_path": "/path/to/local/download/folder",
            "enabled": False,
            "_comment": "Set enabled=true after paths are valid."
        }
    ]
}


@dataclass
class Rule:
    rule_id: str
    source_path: str
    dest_path: str
    enabled: bool


class EventType:
    SCAN = "SCAN"
    DOWNLOAD = "DOWNLOAD"
    REFRESH = "REFRESH"
    ERROR = "ERROR"
    SYSTEM = "SYSTEM"


class EventTypeFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "event_type"):
            record.event_type = EventType.SYSTEM
        return True


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

        self.download_queue: queue.Queue[Tuple[str, str, str]] = queue.Queue()
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
        self.logger = self._setup_logging()

    def _setup_logging(self) -> logging.Logger:
        logger = logging.getLogger("sync_daemon")
        logger.setLevel(logging.INFO)
        logger.handlers.clear()

        formatter = logging.Formatter(
            "%(asctime)s %(levelname)s %(event_type)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )

        event_filter = EventTypeFilter()

        stdout_handler = logging.StreamHandler(sys.stdout)
        stdout_handler.setFormatter(formatter)
        stdout_handler.addFilter(event_filter)

        file_handler = RotatingFileHandler(
            self.log_path,
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.addFilter(event_filter)

        logger.addHandler(stdout_handler)
        logger.addHandler(file_handler)

        return logger

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
        max_workers = int(cfg.get("max_concurrent_downloads", 3))
        max_retry_count = int(cfg.get("max_retry_count", 5))
        bandwidth_limit = float(cfg.get("bandwidth_limit_mbps", 0))
        rclone_command = str(cfg.get("rclone_command", "rclone"))
        rclone_service = str(cfg.get("rclone_service_name", SERVICE_NAME))

        if scan_interval <= 0:
            raise ValueError("scan_interval_seconds must be > 0")
        if refresh_interval <= 0:
            raise ValueError("rclone_refresh_interval_seconds must be > 0")
        if max_workers <= 0:
            raise ValueError("max_concurrent_downloads must be > 0")
        if max_retry_count < 0:
            raise ValueError("max_retry_count must be >= 0")
        if bandwidth_limit < 0:
            raise ValueError("bandwidth_limit_mbps must be >= 0")

        rules_cfg = cfg.get("rules", [])
        if not isinstance(rules_cfg, list):
            raise ValueError("rules must be a list")

        rules = []
        for idx, item in enumerate(rules_cfg):
            if not isinstance(item, dict):
                raise ValueError(f"rules[{idx}] must be an object")
            source_path = str(item.get("source_path", "")).strip()
            dest_path = str(item.get("dest_path", "")).strip()
            enabled = bool(item.get("enabled", False))
            if not source_path or not dest_path:
                raise ValueError(f"rules[{idx}] source_path/dest_path must be non-empty")
            rules.append(
                Rule(
                    rule_id=f"rule_{idx}",
                    source_path=source_path,
                    dest_path=dest_path,
                    enabled=enabled,
                )
            )

        self.config = {
            "scan_interval_seconds": scan_interval,
            "rclone_refresh_interval_seconds": refresh_interval,
            "max_concurrent_downloads": max_workers,
            "max_retry_count": max_retry_count,
            "bandwidth_limit_mbps": bandwidth_limit,
            "rclone_command": rclone_command,
            "rclone_service_name": rclone_service,
        }
        self.rules = rules

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
        self.log_event(EventType.SYSTEM, "signal received, shutting down", signum=signum)
        self.stop_event.set()
        self._drain_pending_queue()

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

        last_scan = 0.0
        last_refresh = time.time()

        self.log_event(EventType.SYSTEM, "daemon started")

        try:
            while not self.stop_event.is_set():
                now = time.time()

                if now - last_refresh >= self.config["rclone_refresh_interval_seconds"]:
                    self.refresh_mount()
                    last_refresh = time.time()

                if not self.pause_event.is_set() and now - last_scan >= self.config["scan_interval_seconds"]:
                    self.incremental_scan()
                    self.enqueue_retry_candidates()
                    last_scan = time.time()

                time.sleep(1)
        finally:
            self.shutdown()

        return 0

    def _initialize_rule_state(self) -> None:
        with self.state_lock:
            rules_state = self.state["rules"]
            for rule in self.rules:
                if rule.rule_id not in rules_state:
                    rules_state[rule.rule_id] = {
                        "source_path": rule.source_path,
                        "dest_path": rule.dest_path,
                        "enabled": rule.enabled,
                        "initialized": False,
                        "files": {},
                    }
                else:
                    rules_state[rule.rule_id]["source_path"] = rule.source_path
                    rules_state[rule.rule_id]["dest_path"] = rule.dest_path
                    rules_state[rule.rule_id]["enabled"] = rule.enabled
                    rules_state[rule.rule_id].setdefault("files", {})
                    rules_state[rule.rule_id].setdefault("initialized", False)
        self.save_state()

    def discover_files(self, source_path: str) -> Dict[str, Dict[str, object]]:
        files = {}
        for root, _dirs, file_names in os.walk(source_path):
            for name in file_names:
                full_path = os.path.join(root, name)
                try:
                    stat = os.stat(full_path)
                except OSError as exc:
                    self.log_error(EventType.ERROR, "failed to stat file", file=full_path, error=str(exc))
                    continue
                files[full_path] = {
                    "size": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                    "last_seen": self.now_iso(),
                }
        return files

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

            discovered = self.discover_files(rule.source_path)

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

            discovered = self.discover_files(rule.source_path)
            new_files = 0

            with self.state_lock:
                rule_state = self.state["rules"][rule.rule_id]
                files_state = rule_state["files"]

                for source_file, meta in discovered.items():
                    if source_file not in files_state:
                        files_state[source_file] = {
                            "size": meta["size"],
                            "mtime_ns": meta["mtime_ns"],
                            "status": "pending",
                            "retry_count": 0,
                            "last_error": None,
                            "last_attempt": None,
                            "last_seen": meta["last_seen"],
                        }
                        new_files += 1
                        self.enqueue_download(rule.rule_id, source_file, rule.dest_path)
                    else:
                        files_state[source_file]["last_seen"] = meta["last_seen"]

                missing_files = [f for f in list(files_state) if f not in discovered]
                for f in missing_files:
                    del files_state[f]

            if new_files > 0 or missing_files:
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
                    if retry_count >= max_retry:
                        continue
                    if self.enqueue_download(rule.rule_id, source_file, rule.dest_path):
                        queued_count += 1

        if queued_count > 0:
            self.log_event(EventType.DOWNLOAD, "retry candidates queued", queued=queued_count)

    def enqueue_download(self, rule_id: str, source_file: str, dest_path: str) -> bool:
        key = self._file_key(rule_id, source_file)

        with self.queue_lock:
            if key in self.queued_files or key in self.in_progress_files:
                return False
            self.download_queue.put((rule_id, source_file, dest_path))
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
                rule_id, source_file, dest_path = self.download_queue.get(timeout=1)
            except queue.Empty:
                continue

            key = self._file_key(rule_id, source_file)
            with self.queue_lock:
                self.queued_files.discard(key)
                self.in_progress_files.add(key)
                self.active_downloads += 1

            try:
                self.handle_download(rule_id, source_file, dest_path)
            finally:
                with self.queue_lock:
                    self.in_progress_files.discard(key)
                    self.active_downloads -= 1
                self.download_queue.task_done()

    def handle_download(self, rule_id: str, source_file: str, dest_path: str) -> None:
        if self.stop_event.is_set():
            return

        command = [self.config["rclone_command"], "copy", source_file, dest_path]
        bandwidth_limit = self.config.get("bandwidth_limit_mbps", 0)
        if bandwidth_limit > 0:
            command.extend(["--bwlimit", f"{bandwidth_limit}M"])
        start_time = time.time()
        self.log_event(EventType.DOWNLOAD, "download started", rule_id=rule_id, source_file=source_file, dest_path=dest_path)

        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=60 * 60,
                check=False,
            )
        except subprocess.TimeoutExpired:
            self.update_download_state(rule_id, source_file, success=False, error="rclone timeout")
            self.log_error(EventType.ERROR, "download timeout", rule_id=rule_id, source_file=source_file)
            return
        except OSError as exc:
            self.update_download_state(rule_id, source_file, success=False, error=str(exc))
            self.log_error(EventType.ERROR, "download process error", rule_id=rule_id, source_file=source_file, error=str(exc))
            return

        duration = round(time.time() - start_time, 3)

        if result.returncode == 0:
            self.update_download_state(rule_id, source_file, success=True, error=None)
            self.log_event(
                EventType.DOWNLOAD,
                "download completed",
                rule_id=rule_id,
                source_file=source_file,
                duration_seconds=duration,
            )
            return

        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        error_text = stderr if stderr else stdout
        self.update_download_state(rule_id, source_file, success=False, error=error_text)
        self.log_error(
            EventType.ERROR,
            "download failed",
            rule_id=rule_id,
            source_file=source_file,
            returncode=result.returncode,
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
        self.pause_event.set()
        self.log_event(EventType.REFRESH, "refresh started", service_name=service_name)

        self.wait_for_download_idle(timeout_seconds=600)

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
                rule_id, source_file, _dest = self.download_queue.get_nowait()
                key = self._file_key(rule_id, source_file)
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
        self._drain_pending_queue()
        self.wait_for_download_idle(timeout_seconds=300)

        for worker in self.workers:
            worker.join(timeout=2)

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
