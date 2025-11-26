#!/usr/bin/env python3

from gevent import monkey
monkey.patch_all()
import sys
import gevent
from gevent.util import format_run_info
from gevent.lock import BoundedSemaphore
from gevent.event import Event
from gevent import Timeout
from gevent.pool import Pool
from gevent.queue import Queue

import argparse
import atexit
import base64
import gzip
import json
import logging
import os
import shutil
import secrets
import signal
import string
import tempfile
import time
import uuid
import weakref
from collections import deque
from dataclasses import dataclass, field
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Optional, Tuple, Any, Dict, Union, List, Callable
from datetime import datetime
from urllib.parse import urlparse, urlunparse

import requests

# ============================================================================
# GLOBAL CONSTANTS AND VARIABLES
# ============================================================================

__version__ = "1.1.10"
letters: str = string.ascii_letters
rand_string: str = ''.join(secrets.choice(letters) for _ in range(10))

ac_str = 'armorcode'

# Temp directory structure using system temp
armorcode_folder: str = os.path.join(tempfile.gettempdir(), ac_str)
log_folder: str = os.path.join(armorcode_folder, 'log')
output_file_folder: str = os.path.join(armorcode_folder, 'output_files')

max_file_size: int = 1024 * 500  # max_size data that would be sent in payload
logger: Optional[logging.Logger] = None

max_retry: int = 3
max_backoff_time: int = 600
min_backoff_time: int = 5

# Global instances
rate_limiter = None
config_dict: dict = None
metrics_logger = None
health_metrics = None


# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================
def print_all_thread_stacks():
    """Print stack traces for all running threads AND greenlets."""
    separator = "=" * 60
    header = "THREAD AND GREENLET STACK TRACES"

    print(f"\n{separator}")
    print(header)
    print(separator)

    if logger:
        logger.warning(separator)
        logger.warning(header)
        logger.warning(separator)

    lines = format_run_info(
        thread_stacks=True,
        greenlet_stacks=True,
        limit=None
    )

    for line in lines:
        clean_line = line.rstrip('\n')
        print(clean_line)
        if logger:
            logger.warning(clean_line)

    print(f"{separator}\n")
    if logger:
        logger.warning(separator)


def str2bool(v):
    """Convert string to boolean for argparse."""
    if isinstance(v, bool):
        return v
    if v is None:
        return True
    if v.lower() in ('yes', 'true', 't', '1'):
        return True
    elif v.lower() in ('no', 'false', 'f', '0'):
        return False
    else:
        raise argparse.ArgumentTypeError('Boolean value expected.')


def _createFolder(folder_path: str) -> None:
    """Create folder if it doesn't exist."""
    if not os.path.exists(folder_path):
        try:
            os.mkdir(folder_path)
            print(f"Created directory: {folder_path}")
        except Exception as e:
            print(f"Error creating folder: {folder_path}, error: {e}")


def generate_unique_id():
    """Generate unique agent ID."""
    timestamp = int(time.time())
    random_hex = uuid.uuid4().hex[:6]
    return f"{timestamp}_{random_hex}"


def _clean_temp_output_files() -> None:
    """Clean up temp output files."""
    if os.path.exists(output_file_folder):
        try:
            for file in os.listdir(output_file_folder):
                file_path = os.path.join(output_file_folder, file)
                if os.path.isfile(file_path):
                    os.remove(file_path)
        except Exception as e:
            print(f"Error cleaning temp output files: {e}")


# ============================================================================
# RATE LIMITER
# ============================================================================

class RateLimiter:
    """Rate limiter to prevent overwhelming the server."""

    def __init__(self, request_limit: int, time_window: int) -> None:
        self.request_limit = request_limit
        self.time_window = time_window
        self.timestamps = deque()
        self.lock = BoundedSemaphore(1)

    def set_limits(self, request_limit: int, time_window: int):
        self.request_limit = request_limit
        self.time_window = time_window

    def allow_request(self) -> bool:
        with self.lock:
            current_time = time.time()
            while self.timestamps and self.timestamps[0] < current_time - self.time_window:
                self.timestamps.popleft()
            if len(self.timestamps) < self.request_limit:
                self.timestamps.append(current_time)
                return True
            return False

    def throttle(self) -> None:
        while not self.allow_request():
            gevent.sleep(0.5)

    def reset(self) -> None:
        """Clear all timestamps - use after pool restart to prevent artificial throttling."""
        with self.lock:
            self.timestamps.clear()
        logger.debug("RateLimiter timestamps cleared")


# ============================================================================
# BUFFERED METRICS LOGGER (TimedRotatingFileHandler)
# ============================================================================

class BufferedMetricsLogger:
    """Buffered metrics logger with file rotation. Disabled by default."""

    def __init__(self, metrics_file: str, flush_interval: int = 10,
                 buffer_size: int = 1000, backup_count: int = 7):
        Path(metrics_file).parent.mkdir(parents=True, exist_ok=True)
        self.flush_interval = flush_interval
        self.buffer_size = buffer_size
        self.backup_count = backup_count
        self.buffer: List[Dict] = []
        self.buffer_lock = BoundedSemaphore(1)
        self.last_flush_time = time.time()
        self.shutdown_flag = Event()

        self.file_logger = logging.getLogger('metrics_file')
        self.file_logger.setLevel(logging.INFO)
        self.file_logger.propagate = False

        handler = TimedRotatingFileHandler(
            metrics_file, when="midnight", interval=1, backupCount=backup_count
        )
        handler.setFormatter(logging.Formatter('%(message)s'))
        self.file_logger.addHandler(handler)

        self.flush_greenlet = gevent.spawn(self._auto_flush_loop)

    def write_metric(self, metric_name: str, value: float, tags: Dict[str, str] = None):
        timestamp_ms = int(time.time() * 1000)
        metric_event = {
            "@timestamp": timestamp_ms,
            "metric_name": metric_name,
            "value": value,
            "tags": tags or {}
        }

        events_to_flush = []
        with self.buffer_lock:
            self.buffer.append(metric_event)
            if len(self.buffer) >= self.buffer_size:
                events_to_flush = self.buffer[:]
                self.buffer.clear()
                self.last_flush_time = time.time()

        if events_to_flush:
            for event in events_to_flush:
                self.file_logger.info(json.dumps(event))

    def _auto_flush_loop(self):
        while not self.shutdown_flag.is_set():
            gevent.sleep(self.flush_interval)

            events_to_flush = []
            with self.buffer_lock:
                if self.buffer and (time.time() - self.last_flush_time) >= self.flush_interval:
                    events_to_flush = self.buffer[:]
                    self.buffer.clear()
                    self.last_flush_time = time.time()

            if events_to_flush:
                for event in events_to_flush:
                    self.file_logger.info(json.dumps(event))

    def flush_now(self):
        events_to_flush = []
        with self.buffer_lock:
            events_to_flush = self.buffer[:]
            self.buffer.clear()
            self.last_flush_time = time.time()

        if events_to_flush:
            for event in events_to_flush:
                self.file_logger.info(json.dumps(event))

    def shutdown(self):
        self.flush_now()
        self.shutdown_flag.set()
        if self.flush_greenlet:
            gevent.joinall([self.flush_greenlet], timeout=5)


# ============================================================================
# HEALTH METRICS (Watchdog State)
# ============================================================================

@dataclass
class HealthMetrics:
    """
    Thread-safe health metrics for watchdog.

    Uses AND logic for unhealthy detection:
    - BOTH conditions must be true:
      1. No get-task call in threshold seconds
      2. No task received in threshold seconds
    """

    _last_get_task_call: Optional[float] = field(default=None, repr=False)
    _last_task_received: Optional[float] = field(default=None, repr=False)
    _last_task_completed: Optional[float] = field(default=None, repr=False)
    _start_time: float = field(default_factory=time.time, repr=False)

    _active_greenlets: int = field(default=0, repr=False)
    _tasks_processed_total: int = field(default=0, repr=False)
    _tasks_failed_total: int = field(default=0, repr=False)
    _pool_restarts: int = field(default=0, repr=False)

    _lock: BoundedSemaphore = field(default_factory=lambda: BoundedSemaphore(1), repr=False)

    # Thresholds (set from config)
    get_task_stale_threshold_sec: int = 3600  # 60 minutes
    task_received_stale_threshold_sec: int = 43200  # 12 hours

    def record_get_task_call(self) -> None:
        with self._lock:
            self._last_get_task_call = time.time()

    def record_task_received(self, task_id: str = None) -> None:
        with self._lock:
            self._last_task_received = time.time()

    def record_task_completed(self, success: bool = True) -> None:
        with self._lock:
            self._last_task_completed = time.time()
            if success:
                self._tasks_processed_total += 1
            else:
                self._tasks_failed_total += 1

    def record_pool_restart(self) -> None:
        with self._lock:
            self._pool_restarts += 1

    def update_active_greenlets(self, count: int) -> None:
        with self._lock:
            self._active_greenlets = count

    def increment_active_greenlets(self) -> None:
        with self._lock:
            self._active_greenlets += 1

    def decrement_active_greenlets(self) -> None:
        with self._lock:
            self._active_greenlets = max(0, self._active_greenlets - 1)

    def is_healthy(self) -> Tuple[bool, List[str]]:
        """Check if worker is healthy (AND logic)."""
        now = time.time()
        reasons = []

        with self._lock:
            get_task_stale = False
            if self._last_get_task_call is not None:
                get_task_age = now - self._last_get_task_call
                if get_task_age > self.get_task_stale_threshold_sec:
                    get_task_stale = True
                    reasons.append(f"get_task_stale: {get_task_age:.0f}s > {self.get_task_stale_threshold_sec}s")
            else:
                uptime = now - self._start_time
                if uptime > self.get_task_stale_threshold_sec:
                    get_task_stale = True
                    reasons.append("get_task_stale: never called")

            task_received_stale = False
            if self._last_task_received is not None:
                task_age = now - self._last_task_received
                if task_age > self.task_received_stale_threshold_sec:
                    task_received_stale = True
                    reasons.append(f"task_received_stale: {task_age:.0f}s > {self.task_received_stale_threshold_sec}s")
            else:
                uptime = now - self._start_time
                if uptime > self.task_received_stale_threshold_sec:
                    task_received_stale = True
                    reasons.append("task_received_stale: never received")

        # AND logic: BOTH must be stale for unhealthy
        is_unhealthy = get_task_stale and task_received_stale
        return (not is_unhealthy, reasons)

    def reset_timestamps(self) -> None:
        """
        Reset timestamps after restart to prevent false-positive unhealthy state.

        Sets timestamps to current time (not None) so threshold checks start fresh.
        This prevents immediate restart loop after a pool restart.
        """
        with self._lock:
            now = time.time()
            self._last_get_task_call = now
            self._last_task_received = now
            self._start_time = now  # Reset uptime counter
        logger.info("HealthMetrics timestamps reset")

    def get_status(self) -> Dict[str, Any]:
        now = time.time()
        with self._lock:
            get_task_age = None
            if self._last_get_task_call:
                get_task_age = now - self._last_get_task_call

            task_received_age = None
            if self._last_task_received:
                task_received_age = now - self._last_task_received

            return {
                'uptime_sec': now - self._start_time,
                'last_get_task_call_age_sec': get_task_age,
                'last_task_received_age_sec': task_received_age,
                'active_greenlets': self._active_greenlets,
                'tasks_processed_total': self._tasks_processed_total,
                'tasks_failed_total': self._tasks_failed_total,
                'pool_restarts': self._pool_restarts,
            }


# ============================================================================
# GREENLET UTILITIES
# ============================================================================

class GreenletTimeoutError(Exception):
    """Raised when a greenlet exceeds its timeout."""
    pass


@dataclass
class GreenletInfo:
    """Tracking information for a spawned greenlet."""
    greenlet_id: int
    task_id: Optional[str]
    start_time: float
    timeout: float
    greenlet_ref: weakref.ref = None

    @property
    def age_seconds(self) -> float:
        return time.time() - self.start_time

    @property
    def is_expired(self) -> bool:
        return self.age_seconds > self.timeout

    @property
    def greenlet(self) -> Optional[Any]:
        if self.greenlet_ref is None:
            return None
        return self.greenlet_ref()


class GreenletManager:
    """Manages greenlet lifecycle with timeout and cleanup."""

    def __init__(self):
        self._registry: Dict[int, GreenletInfo] = {}
        self._lock = BoundedSemaphore(1)
        self._total_spawned = 0
        self._total_timed_out = 0
        self._total_cleaned = 0

    def spawn_with_timeout(
            self,
            pool: Pool,
            func: Callable,
            timeout: float,
            task_id: str = None,
            *args,
            **kwargs
    ) -> 'gevent.Greenlet':
        """Spawn a greenlet with timeout protection."""
        def wrapped():
            greenlet = gevent.getcurrent()
            greenlet_id = id(greenlet)

            try:
                self._register(greenlet_id, task_id, timeout, greenlet)
                with Timeout(timeout, GreenletTimeoutError(
                        f"Task {task_id or greenlet_id} timed out after {timeout}s"
                )):
                    return func(*args, **kwargs)

            except GreenletTimeoutError as e:
                logger.error(f"Greenlet timeout: task={task_id}, timeout={timeout}s")
                self._total_timed_out += 1
                raise

            except Exception as e:
                logger.error(f"Greenlet error: task={task_id}, error={e}")
                raise

            finally:
                self._unregister(greenlet_id)

        greenlet = pool.spawn(wrapped)
        self._total_spawned += 1
        return greenlet

    def _register(self, greenlet_id: int, task_id: Optional[str], timeout: float, greenlet: Any) -> None:
        info = GreenletInfo(
            greenlet_id=greenlet_id,
            task_id=task_id,
            start_time=time.time(),
            timeout=timeout,
            greenlet_ref=weakref.ref(greenlet)
        )
        with self._lock:
            self._registry[greenlet_id] = info

    def _unregister(self, greenlet_id: int) -> None:
        with self._lock:
            self._registry.pop(greenlet_id, None)

    def get_active_greenlets(self) -> List[GreenletInfo]:
        with self._lock:
            return list(self._registry.values())

    def get_dead_greenlets(self, max_age: float = None) -> List[GreenletInfo]:
        dead = []
        with self._lock:
            for gid, info in list(self._registry.items()):
                greenlet = info.greenlet
                if greenlet is None:
                    dead.append(info)
                    continue
                if greenlet.dead:
                    dead.append(info)
                    continue
                if info.is_expired:
                    dead.append(info)
                    continue
                if max_age and info.age_seconds > max_age:
                    dead.append(info)
        return dead

    def cleanup_dead_greenlets(self, max_age: float = None) -> int:
        dead = self.get_dead_greenlets(max_age)
        cleaned = 0

        for info in dead:
            greenlet = info.greenlet
            if greenlet and not greenlet.dead:
                try:
                    logger.warning(
                        f"Killing stuck greenlet: id={info.greenlet_id}, "
                        f"task={info.task_id}, age={info.age_seconds:.1f}s"
                    )
                    greenlet.kill(GreenletTimeoutError("Forced cleanup by GreenletManager"))
                    greenlet.join(timeout=1)
                except Exception as e:
                    logger.error(f"Failed to kill greenlet {info.greenlet_id}: {e}")

            self._unregister(info.greenlet_id)
            cleaned += 1

        if cleaned > 0:
            self._total_cleaned += cleaned
            logger.info(f"Cleaned up {cleaned} dead greenlets")

        return cleaned

    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            active = len(self._registry)
            oldest_age = 0
            if self._registry:
                oldest_age = max(info.age_seconds for info in self._registry.values())

        return {
            'active_greenlets': active,
            'total_spawned': self._total_spawned,
            'total_timed_out': self._total_timed_out,
            'total_cleaned': self._total_cleaned,
            'oldest_greenlet_age_sec': oldest_age
        }

    def shutdown(self) -> None:
        logger.info("GreenletManager shutting down")
        with self._lock:
            greenlets_to_kill = list(self._registry.values())

        for info in greenlets_to_kill:
            greenlet = info.greenlet
            if greenlet and not greenlet.dead:
                try:
                    greenlet.kill()
                except Exception as e:
                    logger.error(f"Failed to kill greenlet during shutdown: {e}")

        with self._lock:
            self._registry.clear()

    def reset(self) -> None:
        """
        Full reset - clear registry and reset all stats.

        Use during worker restart to get a completely fresh state.
        Unlike shutdown(), this doesn't kill greenlets (they should already be dead).
        """
        with self._lock:
            self._registry.clear()
            self._total_spawned = 0
            self._total_timed_out = 0
            self._total_cleaned = 0
        logger.info("GreenletManager fully reset (registry + stats)")


# ============================================================================
# METRICS HELPER FUNCTIONS
# ============================================================================

def _get_url_without_params(url: str) -> str:
    """Remove query parameters from URL."""
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, '', '', ''))


def _safe_parse_url(url: str, default_domain: str = "unknown") -> Tuple[str, str]:
    try:
        if url is None or not isinstance(url, str):
            return "unknown", default_domain
        parsed = urlparse(url)
        clean_url = urlunparse((parsed.scheme, parsed.netloc, parsed.path, '', '', ''))
        domain = parsed.netloc if parsed.netloc else default_domain
        return clean_url, domain
    except Exception:
        return "unknown", default_domain


def _safe_log_metric(metric_name: str, value: float, tags: Dict[str, str] = None) -> None:
    """Safe metric logging - no-op if metrics disabled."""
    try:
        if metrics_logger is not None:
            metrics_logger.write_metric(metric_name, value, tags)
    except Exception as e:
        if logger:
            logger.debug(f"Metrics logging failed: {e}")


def _build_http_request_tags(
        task_id: str,
        operation: str,
        url: str,
        method: str,
        status_code: str,
        **extra_tags
) -> Dict[str, str]:
    try:
        clean_url, domain = _safe_parse_url(url)
        tags = {
            "task_id": str(task_id) if task_id else "none",
            "operation": operation,
            "url": clean_url,
            "domain": domain,
            "method": method,
            "status_code": str(status_code)
        }
        tags.update(extra_tags)
        return tags
    except Exception:
        return {"error": "tag_build_failed"}


def _build_task_processing_tags(
        task_id: str,
        method: str,
        url: str,
        http_status: Union[int, str]
) -> Dict[str, str]:
    try:
        _, domain = _safe_parse_url(url)
        return {
            "task_id": str(task_id) if task_id else "unknown",
            "method": method,
            "domain": domain,
            "http_status": str(http_status)
        }
    except Exception:
        return {"error": "tag_build_failed"}


def _build_upload_tags(task_id: str, upload_type: str) -> Dict[str, str]:
    try:
        return {
            "task_id": str(task_id) if task_id else "unknown",
            "upload_type": upload_type
        }
    except Exception:
        return {"error": "tag_build_failed"}


def _log_get_task_metric(
        duration_ms: float,
        server_url: str,
        status_code: Union[int, str],
        task: Optional[Dict[str, Any]] = None
) -> None:
    try:
        task_id = task.get('taskId', 'none') if task else "none"
        has_task = str(task is not None).lower()

        tags = _build_http_request_tags(
            task_id=task_id,
            operation="get_task",
            url=server_url,
            method="GET",
            status_code=str(status_code),
            has_task=has_task
        )
        _safe_log_metric("http.request.duration_ms", duration_ms, tags)
    except Exception as e:
        if logger:
            logger.debug(f"Failed to log get-task metric: {e}")


# ============================================================================
# HTTP HEADERS
# ============================================================================

def _get_headers() -> Dict[str, str]:
    """Get default HTTP headers with authorization."""
    headers: Dict[str, str] = {
        "Authorization": f"Bearer {config_dict['api_key']}",
        "Content-Type": "application/json"
    }
    return headers


# ============================================================================
# TASK PROCESSING
# ============================================================================

def check_and_update_encode_url(headers, url: str):
    """Update headers for specific URL patterns."""
    if "/cxrestapi/auth/identity/connect/token" in url:
        headers["Content-Type"] = "application/x-www-form-urlencoded"


def check_for_logs_fetch(url, task, temp_output_file_zip):
    """Handle special log fetch requests."""
    if 'agent/fetch-logs' in url and 'fetchLogs' in task.get('taskId'):
        try:
            shutil.make_archive(temp_output_file_zip.name[:-4], 'zip', log_folder)
            task['responseZipped'] = True
            headers: Dict[str, str] = {
                "Authorization": f"Bearer {config_dict['api_key']}",
            }
            logger.info(f"Logs zipped successfully: {temp_output_file_zip.name}")
            task_json = json.dumps(task)
            files = {
                "file": (temp_output_file_zip.name, open(temp_output_file_zip.name, "rb"), "application/zip"),
                "task": (None, task_json, "application/json")
            }
            rate_limiter.throttle()
            upload_logs_url = f"{config_dict.get('server_url')}/api/http-teleport/upload-logs"
            if len(config_dict.get('env_name', '')) > 0:
                upload_logs_url = f"{config_dict.get('server_url')}/api/http-teleport/upload-logs?envName={config_dict.get('env_name')}"
            upload_result: requests.Response = requests.post(
                upload_logs_url,
                headers=headers,
                timeout=300, verify=config_dict.get('verify_cert', False), proxies=config_dict['outgoing_proxy'],
                files=files
            )
            if upload_result.status_code == 200:
                return True
            else:
                logger.error(f"Response code while uploading is not 200, response code {upload_result.status_code}")
            return True
        except Exception as e:
            logger.error(f"Error zipping logs: {str(e)}")
            raise e
    return False


def zip_response(temp_file, temp_file_zip) -> bool:
    """Gzip compress a file."""
    try:
        if not (Path(temp_file).is_relative_to(tempfile.gettempdir()) and
                Path(temp_file_zip).is_relative_to(tempfile.gettempdir())):
            raise ValueError("Files must be within the allowed directory")

        chunk_size = 1024 * 1024
        with open(temp_file, 'rb') as f_in:
            with gzip.open(temp_file_zip, 'wb') as f_out:
                while True:
                    chunk = f_in.read(chunk_size)
                    if not chunk:
                        break
                    f_out.write(chunk)

        return True
    except Exception as e:
        logger.error("Unable to zip file: %s", e)
        return False


def get_s3_upload_url(taskId: str) -> Tuple[Optional[str], Optional[str]]:
    """Get pre-signed S3 upload URL."""
    params: Dict[str, str] = {'fileName': f"{taskId}{uuid.uuid4().hex}"}
    try:
        rate_limiter.throttle()
        get_s3_url: requests.Response = requests.get(
            f"{config_dict.get('server_url')}/api/http-teleport/upload-url",
            params=params,
            headers=_get_headers(),
            timeout=25, verify=config_dict.get('verify_cert', False), proxies=config_dict['outgoing_proxy']
        )
        get_s3_url.raise_for_status()

        data: Optional[Dict[str, str]] = get_s3_url.json().get('data', None)
        if data is not None:
            return data.get('putUrl'), data.get('getUrl')
        logger.warning("No data returned when requesting S3 upload URL")
        return None, None
    except requests.exceptions.RequestException as e:
        logger.error("Network error getting S3 upload URL: %s", e)
    except Exception as e:
        logger.exception("Unexpected error getting S3 upload URL: %s", e)
    return None, None


def upload_s3(temp_file, preSignedUrl: str, headers: Dict[str, Any]) -> bool:
    """Upload file to S3."""
    headersForS3: Dict[str, str] = {}
    if 'Content-Encoding' in headers and headers['Content-Encoding'] is not None:
        headersForS3['Content-Encoding'] = headers['Content-Encoding']
    if 'Content-Type' in headers and headers['Content-Type'] is not None:
        headersForS3['Content-Type'] = headers['Content-Type']

    try:
        with open(temp_file, 'rb') as file:
            response: requests.Response = requests.put(
                preSignedUrl, headers=headersForS3, data=file,
                verify=config_dict.get('verify_cert', False),
                proxies=config_dict['outgoing_proxy'], timeout=120
            )
            response.raise_for_status()
            logger.info('File uploaded successfully to S3')
            return True
    except requests.exceptions.RequestException as e:
        logger.error("Network error uploading to S3: %s", e)
        raise
    except Exception as e:
        logger.exception("Unexpected error uploading to S3: %s", e)
        raise


def upload_response(temp_file, temp_file_zip, taskId: str, task: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Upload response to ArmorCode or S3."""
    if config_dict.get('upload_to_ac', True):
        max_retries = 5
        retry_delay = 1  # Initial delay in seconds (exponential backoff: 1, 2, 4, 8)

        success = zip_response(temp_file, temp_file_zip)
        file_path = temp_file_zip if success else temp_file
        task['responseZipped'] = success
        file_name = f"{taskId}_{uuid.uuid4().hex}.{'zip' if success else 'txt'}"
        file_size = os.path.getsize(file_path)
        auth_headers: Dict[str, str] = {
            "Authorization": f"Bearer {config_dict['api_key']}",
        }
        task_json = json.dumps(task)
        content_type = 'application/zip' if success else 'text/plain'
        upload_url = f"{config_dict.get('server_url')}/api/http-teleport/upload-result"

        last_exception = None
        for attempt in range(1, max_retries + 1):
            try:
                # Reopen file on each attempt (file pointer resets)
                with open(file_path, "rb") as f:
                    files = {
                        "file": (file_name, f, content_type),
                        "task": (None, task_json, "application/json")
                    }
                    rate_limiter.throttle()
                    upload_start_time = time.time()
                    upload_result: requests.Response = requests.post(
                        upload_url,
                        headers=auth_headers,
                        timeout=300, verify=config_dict.get('verify_cert', False), proxies=config_dict['outgoing_proxy'],
                        files=files
                    )
                    upload_duration_ms = (time.time() - upload_start_time) * 1000

                tags = _build_http_request_tags(
                    task_id=taskId,
                    operation="upload_file",
                    url=upload_url,
                    method="POST",
                    status_code=str(upload_result.status_code),
                    success=str(upload_result.status_code < 400).lower()
                )
                _safe_log_metric("http.request.duration_ms", upload_duration_ms, tags)

                tags = _build_upload_tags(taskId, "direct")
                _safe_log_metric("upload.size_bytes", file_size, tags)

                logger.info("Upload result response: %s, code: %d (attempt %d/%d)",
                            upload_result.text, upload_result.status_code, attempt, max_retries)

                # Check for 429 rate limit - retry if not last attempt
                if upload_result.status_code == 429:
                    if attempt < max_retries:
                        logger.warning("Upload rate limited (429), retrying in %ds (attempt %d/%d)",
                                       retry_delay, attempt, max_retries)
                        _safe_log_metric("upload.retry", 1, {"task_id": taskId, "reason": "rate_limit_429", "attempt": str(attempt)})
                        time.sleep(retry_delay)
                        retry_delay *= 2  # Exponential backoff
                        continue
                    else:
                        logger.error("Upload rate limited (429), max retries exhausted")
                        upload_result.raise_for_status()

                upload_result.raise_for_status()
                return None

            except Exception as e:
                last_exception = e
                if attempt < max_retries:
                    logger.warning("Upload failed (attempt %d/%d): %s, retrying in %ds",
                                   attempt, max_retries, e, retry_delay)
                    _safe_log_metric("upload.retry", 1, {"task_id": taskId, "reason": "exception", "attempt": str(attempt)})
                    time.sleep(retry_delay)
                    retry_delay *= 2  # Exponential backoff
                else:
                    logger.error("Unable to upload file to armorcode after %d attempts: %s", max_retries, e)
                    raise e

        # Should not reach here, but safety net
        if last_exception:
            raise last_exception
    else:
        s3_upload_url, s3_signed_get_url = get_s3_upload_url(taskId)
        if s3_upload_url is not None:
            upload_success = upload_s3(temp_file, s3_upload_url, task['responseHeaders'])
            if upload_success:
                task['s3Url'] = s3_signed_get_url
                logger.info("Data uploaded to S3 successfully")
                return task

        task['status'] = 500
        task['output'] = "Error: failed to upload result to s3"
        return task


def process_task(task: Dict[str, Any]) -> Dict[str, Any]:
    """Process a single task - full implementation from original worker.py."""
    url: str = task.get('url')
    input_data: Any = task.get('input')
    taskId: str = task.get('taskId')
    headers: Dict[str, str] = task.get('requestHeaders', {})
    method: str = task.get('method').upper()
    expiryTime: int = task.get('expiryTsMs', round((time.time() + 300) * 1000))
    logger.info("Processing task %s: %s %s", taskId, method, url)

    task_start_time = time.time()

    _createFolder(log_folder)
    _createFolder(output_file_folder)
    temp_output_file = tempfile.NamedTemporaryFile(
        prefix="output_file" + taskId,
        suffix=".txt",
        dir=output_file_folder,
        delete=False
    )

    temp_output_file_zip = tempfile.NamedTemporaryFile(
        prefix="output_file_zip" + taskId,
        suffix=".zip",
        dir=output_file_folder,
        delete=False
    )

    try:
        if task.get('globalConfig', None) is not None:
            global_config = task.get('globalConfig', {})
            update_agent_config(global_config)
        timeout = round((expiryTime - round(time.time() * 1000)) / 1000)
        logger.info("expiry %s, %s", expiryTime, timeout)

        logger.info("Request for task %s with and input_data %s", taskId, input_data)

        if check_for_logs_fetch(url, task, temp_output_file_zip):
            return None
        check_and_update_encode_url(headers, url)
        encoded_input_data = input_data
        if isinstance(input_data, str):
            encoded_input_data = input_data.encode('utf-8')
        elif isinstance(input_data, bytes):
            encoded_input_data = input_data
        else:
            logger.debug("Input data is not str or bytes %s", input_data)

        http_start_time = time.time()
        # Use inward_proxy for target URL requests
        response: requests.Response = requests.request(
            method, url, headers=headers, data=encoded_input_data, stream=True,
            timeout=(15, timeout), verify=config_dict.get('verify_cert'),
            proxies=config_dict['inward_proxy']
        )
        http_duration_ms = (time.time() - http_start_time) * 1000
        logger.info("Response: %d", response.status_code)

        tags = _build_http_request_tags(
            task_id=taskId,
            operation="target_request",
            url=url,
            method=method,
            status_code=str(response.status_code),
            success=str(response.status_code < 400).lower()
        )
        _safe_log_metric("http.request.duration_ms", http_duration_ms, tags)

        data: Any = None
        if response.status_code == 200:
            is_chunked: bool = response.headers.get('Transfer-Encoding', None) == 'chunked'

            if is_chunked:
                logger.info("Processing in chunks...")
                with open(temp_output_file.name, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=1024 * 100):
                        if chunk:
                            f.write(chunk)
            else:
                logger.info("Non-chunked response, processing whole payload...")
                with open(temp_output_file.name, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=1024 * 500):
                        f.write(chunk)
        else:
            logger.info("Status code is not 200 , response is %s", response.content)
            data = response.content
            with open(temp_output_file.name, 'wb') as f:
                f.write(data)

        task['responseHeaders'] = dict(response.headers)
        task['statusCode'] = response.status_code

        file_size: int = os.path.getsize(temp_output_file.name)
        logger.info("file size %s", file_size)
        is_s3_upload: bool = file_size > max_file_size

        if not is_s3_upload:
            logger.info("Data is less than %s, sending data in response", max_file_size)
            with open(temp_output_file.name, 'rb') as file:
                file_data = file.read()
                if len(file_data) == 0:
                    return task
                base64_string = base64.b64encode(file_data).decode('utf-8')
                task['responseBase64'] = True
                task['output'] = base64_string

            tags = _build_upload_tags(taskId, "inline")
            _safe_log_metric("upload.size_bytes", file_size, tags)

            return task

        return upload_response(temp_output_file.name, temp_output_file_zip.name, taskId, task)
    except requests.exceptions.RequestException as e:
        logger.error("Network error processing task %s: %s", taskId, e)
        task['statusCode'] = 500
        task['output'] = f"Agent Side Error: Network error: {str(e)}"
    except Exception as e:
        logger.error("Unexpected error processing task %s: %s", taskId, e)
        task['statusCode'] = 500
        task['output'] = f"Agent Side Error: Error: {str(e)}"
    finally:
        task_total_duration_ms = (time.time() - task_start_time) * 1000
        tags = _build_task_processing_tags(taskId, method, url, task.get('statusCode', 'unknown'))
        _safe_log_metric("task.processing_duration_ms", task_total_duration_ms, tags)

        temp_output_file.close()
        temp_output_file_zip.close()
        os.unlink(temp_output_file.name)
        os.unlink(temp_output_file_zip.name)
    return task


def _log_update_metrics(task: Dict[str, Any], response: requests.Response, duration_ms: float) -> None:
    """Log metrics for update_task operation."""
    try:
        tags = _build_http_request_tags(
            task_id=task.get('taskId', 'unknown'),
            operation="upload_result",
            url=f"{config_dict.get('server_url')}/api/http-teleport/put-result",
            method="POST",
            status_code=response.status_code,
            success=str(response.status_code == 200).lower()
        )

        if response.status_code == 429:
            tags["error_type"] = "rate_limit"
        elif response.status_code == 504:
            tags["error_type"] = "timeout"
        elif response.status_code >= 500:
            tags["error_type"] = "server_error"
        elif response.status_code >= 400:
            tags["error_type"] = "client_error"

        _safe_log_metric("http.request.duration_ms", duration_ms, tags)
    except Exception as e:
        if logger:
            logger.debug(f"Failed to log update metrics: {e}")


def update_task(task: Optional[Dict[str, Any]], count: int = 0) -> None:
    """Update task result to server."""
    if task is None:
        return
    if count > max_retry:
        logger.error("Retry count exceeds for task %s", task['taskId'])
        return
    try:
        rate_limiter.throttle()
        update_start_time = time.time()
        update_task_response: requests.Response = requests.post(
            f"{config_dict.get('server_url')}/api/http-teleport/put-result",
            headers=_get_headers(),
            json=task,
            timeout=30, verify=config_dict.get('verify_cert'), proxies=config_dict['outgoing_proxy']
        )
        update_duration_ms = (time.time() - update_start_time) * 1000

        _log_update_metrics(task, update_task_response, update_duration_ms)

        if update_task_response.status_code == 200:
            logger.info("Task %s updated successfully. Response: %s", task['taskId'],
                        update_task_response.text)
        elif update_task_response.status_code == 429 or update_task_response.status_code == 504:
            gevent.sleep(2)
            logger.warning("Rate limit hit while updating the task output, retrying again for task %s", task['taskId'])
            count = count + 1
            update_task(task, count)
        else:
            logger.warning("Failed to update task %s: %s", task['taskId'], update_task_response.text)

    except requests.exceptions.RequestException as e:
        logger.error("Network error processing task %s: %s", task['taskId'], e)
        count = count + 1
        update_task(task, count)


def process_task_async(task: Dict[str, Any]) -> None:
    """Process task asynchronously."""
    url: str = task.get('url')
    taskId: str = task.get('taskId')
    method: str = task.get('method').upper()

    try:
        result: Dict[str, Any] = process_task(task)
        update_task(result)
    except Exception as e:
        logger.info("Unexpected error while processing task id: %s, method: %s url: %s, error: %s",
                    taskId, method, url, e)


def update_agent_config(global_config: dict[str, Any]) -> None:
    """Update agent configuration from server."""
    global config_dict, rate_limiter
    if global_config.get("debugMode") is not None:
        if global_config.get("debugMode", False):
            logger.setLevel(logging.DEBUG)
        else:
            logger.setLevel(logging.INFO)

    if global_config.get("verifyCert", False):
        config_dict['verify_cert'] = global_config.get("verifyCert", False)
    if global_config.get("threadPoolSize", 5):
        config_dict['thread_pool_size'] = global_config.get("poolSize", 5)
        config_dict['thread_pool'] = Pool(config_dict['thread_pool_size'])
    if global_config.get("uploadToAC") is not None:
        config_dict['upload_to_ac'] = global_config.get("uploadToAC", True)
    if global_config.get("rateLimitPerMin", 500):
        rate_limiter.set_limits(global_config.get("rateLimitPerMin", 100), 60)


# ============================================================================
# LOGGER SETUP
# ============================================================================

def setup_logger(index: str, debug_mode: bool, enable_stdout: bool = False) -> logging.Logger:
    """Set up logging with timed rotation."""
    log_filename: str = os.path.join(log_folder, f"app_log{index}.log")

    file_handler: TimedRotatingFileHandler = TimedRotatingFileHandler(
        log_filename, when="midnight", interval=1, backupCount=7
    )

    formatter: logging.Formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    file_handler.setFormatter(formatter)

    logger: logging.Logger = logging.getLogger(__name__)
    if debug_mode:
        logger.setLevel(logging.DEBUG)
    else:
        logger.setLevel(logging.INFO)

    logger.handlers.clear()
    logger.addHandler(file_handler)

    if enable_stdout:
        console_handler: logging.StreamHandler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

    logger.info("Log folder is created %s", log_folder)
    return logger


# ============================================================================
# CONFIGURATION PARSING (argparse)
# ============================================================================

def get_initial_config(parser) -> tuple:
    """Parse command line arguments and environment variables."""
    global rate_limiter
    config = {
        "api_key": None,
        "server_url": None,
        "verify_cert": True,
        "timeout": 10,
        "inward_proxy": None,
        "outgoing_proxy": None,
        "upload_to_ac": False,
        "env_name": None,
        "thread_pool_size": 5
    }

    parser.add_argument("--serverUrl", required=False, help="Server Url")
    parser.add_argument("--apiKey", required=False, help="Api Key")
    parser.add_argument("--index", required=False, help="Agent index no", default="_prod")
    parser.add_argument("--timeout", required=False, help="timeout", default=30)
    parser.add_argument("--verify", required=False, help="Verify Cert", default=False)
    parser.add_argument("--debugMode", required=False, help="Enable debug Mode", default=False)
    parser.add_argument("--envName", required=False, help="Environment name", default="")

    parser.add_argument("--inwardProxyHttps", required=False, help="Pass inward Https proxy", default=None)
    parser.add_argument("--inwardProxyHttp", required=False, help="Pass inward Http proxy", default=None)

    parser.add_argument("--outgoingProxyHttps", required=False, help="Pass outgoing Https proxy", default=None)
    parser.add_argument("--outgoingProxyHttp", required=False, help="Pass outgoing Http proxy", default=None)
    parser.add_argument("--poolSize", required=False, help="Multi threading pool size", default=5)
    parser.add_argument("--rateLimitPerMin", required=False, help="Rate limit per min", default=250)
    parser.add_argument("--metricsRetentionDays", required=False, type=int, help="Metrics log retention in days", default=7)

    parser.add_argument(
        "--uploadToAc",
        nargs='?',
        type=str2bool,
        const=True,
        default=True,
        help="Upload to Armorcode instead of S3 (default: True)"
    )

    parser.add_argument(
        "--enableStdoutLogging",
        nargs='?',
        type=str2bool,
        const=True,
        default=False,
        help="Enable logging to stdout/console in addition to file (default: False)"
    )

    # NEW: --metrics flag to enable/disable metrics (DISABLED by default)
    parser.add_argument(
        "--metrics",
        nargs='?',
        type=str2bool,
        const=True,
        default=False,
        help="Enable metrics logging (default: disabled)"
    )

    # Watchdog thresholds
    parser.add_argument("--getTaskStaleThreshold", required=False, type=int, default=3600,
                        help="Get-task stale threshold in seconds (default: 3600 = 60 min)")
    parser.add_argument("--taskReceivedStaleThreshold", required=False, type=int, default=43200,
                        help="Task received stale threshold in seconds (default: 43200 = 12 hours)")

    args = parser.parse_args()
    config['agent_id'] = generate_unique_id()
    config['server_url'] = args.serverUrl
    config['api_key'] = args.apiKey
    agent_index: str = args.index + config['agent_id']
    timeout_cmd = args.timeout
    pool_size_cmd = args.poolSize
    verify_cmd = args.verify
    debug_cmd = args.debugMode
    rate_limit_per_min = args.rateLimitPerMin
    config['metrics_retention_days'] = args.metricsRetentionDays
    config['metrics_enabled'] = args.metrics

    config['upload_to_ac'] = args.uploadToAc
    enable_stdout_logging_cmd = args.enableStdoutLogging

    # Watchdog thresholds
    config['get_task_stale_threshold'] = args.getTaskStaleThreshold
    config['task_received_stale_threshold'] = args.taskReceivedStaleThreshold

    rate_limiter.set_limits(rate_limit_per_min, 60)
    inward_proxy_https = args.inwardProxyHttps
    inward_proxy_http = args.inwardProxyHttp

    outgoing_proxy_https = args.outgoingProxyHttps
    outgoing_proxy_http = args.outgoingProxyHttp
    config['env_name'] = args.envName

    # Inward proxy (for target URL requests)
    if inward_proxy_https is None and inward_proxy_http is None:
        config['inward_proxy'] = None
    else:
        inward_proxy = {}
        if inward_proxy_https is not None:
            inward_proxy['https'] = inward_proxy_https
        if inward_proxy_http is not None:
            inward_proxy['http'] = inward_proxy_http
        config['inward_proxy'] = inward_proxy

    # Outgoing proxy (for ArmorCode API calls)
    if outgoing_proxy_https is None and outgoing_proxy_http is None:
        config['outgoing_proxy'] = None
    else:
        outgoing_proxy = {}
        if outgoing_proxy_https is not None:
            outgoing_proxy['https'] = outgoing_proxy_https
        if outgoing_proxy_http is not None:
            outgoing_proxy['http'] = outgoing_proxy_http
        config['outgoing_proxy'] = outgoing_proxy

    debug_mode = False
    if debug_cmd is not None:
        if str(debug_cmd).lower() == "true":
            debug_mode = True

    enable_stdout_logging = enable_stdout_logging_cmd if isinstance(enable_stdout_logging_cmd, bool) else False

    if verify_cmd is not None:
        if str(verify_cmd).lower() == "false":
            config['verify_cert'] = False

    if timeout_cmd is not None:
        config['timeout'] = int(timeout_cmd)
    if pool_size_cmd is not None:
        config['thread_pool_size'] = int(pool_size_cmd)
    if os.getenv('verify') is not None:
        if str(os.getenv('verify')).lower() == "true":
            config['verify_cert'] = True

    if os.getenv("timeout") is not None:
        config['timeout'] = int(os.getenv("timeout"))

    if os.getenv("metricsRetentionDays") is not None:
        config['metrics_retention_days'] = int(os.getenv("metricsRetentionDays"))

    # Fallback to environment variables if not provided as arguments
    if config.get('server_url', None) is None:
        config['server_url'] = os.getenv('server_url')
    if config.get('api_key', None) is None:
        config['api_key'] = os.getenv("api_key")
    config['thread_pool'] = Pool(config.get('thread_pool_size', 5))
    return config, agent_index, debug_mode, enable_stdout_logging


# ============================================================================
# WATCHDOG WORKER CLASS
# ============================================================================

class WatchdogWorker:
    """
    In-Process Watchdog Worker.

    Combines:
    - Original worker.py task processing functionality
    - Watchdog greenlet for health monitoring
    - Deadlock prevention via keepalive heartbeat
    """

    def __init__(self):
        self.greenlet_manager = GreenletManager()
        self.task_queue: Queue = Queue(maxsize=config_dict['thread_pool_size'] * 2)
        self.pool: Optional[Pool] = None

        self._shutdown_event = Event()
        self._restart_event = Event()

        self._watchdog_greenlet = None
        self._get_task_greenlet = None
        self._queue_processor_greenlet = None

        self._restart_count = 0
        self._start_time = time.time()

    def start(self) -> None:
        """Start the worker with watchdog."""
        logger.info(f"Starting WatchdogWorker... id(self)={id(self)}")

        self._create_pool()

        # Spawn watchdog greenlet (keeps hub alive)
        logger.info(f"Spawning watchdog greenlet: id(self)={id(self)}, id(_watchdog_loop)={id(self._watchdog_loop)}")
        self._watchdog_greenlet = gevent.spawn(self._watchdog_loop)

        # Spawn get-task greenlet
        logger.info(f"Spawning get_task greenlet: id(self)={id(self)}, id(_get_task_loop)={id(self._get_task_loop)}")
        self._get_task_greenlet = gevent.spawn(self._get_task_loop)

        # Spawn queue processor greenlet
        logger.info(f"Spawning queue_processor greenlet: id(self)={id(self)}, id(_queue_processor_loop)={id(self._queue_processor_loop)}")
        self._queue_processor_greenlet = gevent.spawn(self._queue_processor_loop)

        logger.info(f"WatchdogWorker started - all greenlets running. id(self)={id(self)}, pool={self.pool}, id(pool)={id(self.pool) if self.pool else 'None'}")

        # Main event loop (provides heartbeat)
        self._main_event_loop()

    def _create_pool(self) -> None:
        logger.info(f"_create_pool called: id(self)={id(self)}, current pool={self.pool}")
        if self.pool:
            self._destroy_pool()

        self.pool = Pool(size=config_dict['thread_pool_size'])
        logger.info(f"Created worker pool with size {config_dict['thread_pool_size']}, id(self)={id(self)}, id(pool)={id(self.pool)}")

    def _destroy_pool(self) -> None:
        if self.pool is None:
            return

        logger.info("Destroying worker pool...")
        self.greenlet_manager.shutdown()

        try:
            self.pool.kill(timeout=30)
        except Exception as e:
            logger.error(f"Error killing pool: {e}")

        self.pool = None

    def _main_event_loop(self) -> None:
        """
        Main event loop - provides heartbeat that prevents LoopExit.

        OPTIMIZATION: Uses event.wait(timeout=5) instead of gevent.sleep(1).

        Benefits:
        1. Reduced frequency: 5s vs 1s = 5x fewer context switches
        2. Immediate shutdown response: wait() returns immediately when event is set
        3. Same keepalive effect: event.wait() still yields to gevent hub

        The 1-second interval was too frequent for just checking restart/shutdown flags.
        5 seconds is a good balance between responsiveness and efficiency.
        """
        heartbeat_interval: int = 60  # Check every 60 seconds

        while not self._shutdown_event.is_set():
            if self._restart_event.is_set():
                self._handle_restart()
                self._restart_event.clear()

            # Use event.wait() with timeout - returns immediately on shutdown signal
            # This is more efficient than gevent.sleep() and responds faster to shutdown
            self._shutdown_event.wait(timeout=heartbeat_interval)

        self._shutdown()

    def _handle_restart(self) -> None:
        """
        Handle FULL worker restart with proper state cleanup.

        Resets ALL greenlets:
        - watchdog greenlet (kill and respawn)
        - get_task greenlet (kill and respawn)
        - queue_processor greenlet (kill and respawn)
        - Worker pool (kill all task greenlets)

        Resets ALL state:
        - task_queue (drain all pending tasks)
        - RateLimiter timestamps
        - HealthMetrics timestamps
        - GreenletManager registry + stats
        - Metrics buffer (flush before reset)
        """
        logger.warning("Handling FULL worker restart request...")
        self._restart_count += 1

        # ==================== PRE-RESET: PRESERVE DATA ====================

        # 1. Flush metrics buffer before restart (preserve data)
        if metrics_logger is not None:
            metrics_logger.flush_now()

        # 2. Log restart metric BEFORE reset (so it's captured)
        health_metrics.record_pool_restart()
        _safe_log_metric('pool.restart', 1, {
            'restart_count': str(self._restart_count),
            'reason': 'watchdog_triggered'
        })

        # ==================== KILL ALL GREENLETS ====================

        # 3. Kill watchdog greenlet
        if self._watchdog_greenlet:
            logger.info("Killing watchdog greenlet...")
            self._watchdog_greenlet.kill()
            self._watchdog_greenlet = None

        # 4. Kill get_task greenlet
        if self._get_task_greenlet:
            logger.info("Killing get_task greenlet...")
            self._get_task_greenlet.kill()
            self._get_task_greenlet = None

        # 5. Kill queue_processor greenlet
        if self._queue_processor_greenlet:
            logger.info("Killing queue_processor greenlet...")
            self._queue_processor_greenlet.kill()
            self._queue_processor_greenlet = None

        # 6. Destroy pool (kills task greenlets)
        self._destroy_pool()

        # ==================== RESET ALL STATE ====================

        # 7. Drain task_queue
        drained_count = 0
        while not self.task_queue.empty():
            try:
                self.task_queue.get_nowait()
                drained_count += 1
            except Exception:
                break
        if drained_count > 0:
            logger.warning(f"Drained {drained_count} tasks from queue during restart")

        # 8. Reset RateLimiter
        rate_limiter.reset()

        # 9. Reset HealthMetrics
        health_metrics.reset_timestamps()

        # 10. Reset GreenletManager (registry + stats)
        self.greenlet_manager.reset()

        # ==================== RESPAWN ALL GREENLETS ====================

        # 11. Create new pool
        self._create_pool()

        # 12. Respawn watchdog greenlet
        logger.info("Respawning watchdog greenlet...")
        self._watchdog_greenlet = gevent.spawn(self._watchdog_loop)

        # 13. Respawn get_task greenlet
        logger.info("Respawning get_task greenlet...")
        self._get_task_greenlet = gevent.spawn(self._get_task_loop)

        # 14. Respawn queue_processor greenlet
        logger.info("Respawning queue_processor greenlet...")
        self._queue_processor_greenlet = gevent.spawn(self._queue_processor_loop)

        logger.info(f"FULL worker restart complete (restart #{self._restart_count})")

    def _shutdown(self) -> None:
        logger.info("Starting graceful shutdown...")

        if self._watchdog_greenlet:
            self._watchdog_greenlet.kill()
        if self._get_task_greenlet:
            self._get_task_greenlet.kill()
        if self._queue_processor_greenlet:
            self._queue_processor_greenlet.kill()

        self._destroy_pool()

        logger.info("Graceful shutdown complete")

    def trigger_shutdown(self) -> None:
        self._shutdown_event.set()

    # ==================== WATCHDOG GREENLET ====================

    def _watchdog_loop(self) -> None:
        """Watchdog greenlet - monitors health and triggers restarts."""
        logger.info("Watchdog greenlet started")
        watchdog_interval = 60  # Check every 60 seconds

        while not self._shutdown_event.is_set():
            try:
                is_healthy, reasons = health_metrics.is_healthy()

                if not is_healthy:
                    logger.warning(f"Worker unhealthy: {reasons}")
                    _safe_log_metric('watchdog.unhealthy', 1, {
                        'reasons': ','.join(reasons)
                    })
                    self._restart_event.set()
                else:
                    logger.debug("Watchdog: health check passed")

                # Cleanup dead greenlets
                cleaned = self.greenlet_manager.cleanup_dead_greenlets()
                if cleaned > 0:
                    _safe_log_metric('greenlet.cleanup', cleaned)

                health_metrics.update_active_greenlets(
                    len(self.greenlet_manager.get_active_greenlets())
                )

                status = health_metrics.get_status()
                logger.debug(f"Health status: {status}")

            except Exception as e:
                logger.error(f"Watchdog error: {e}")
                _safe_log_metric('watchdog.error', 1, {'error': str(e)})

            gevent.sleep(watchdog_interval)

        logger.info("Watchdog greenlet stopped")

    # ==================== GET-TASK GREENLET ====================

    def _fetch_task_once(self) -> Optional[Tuple[Optional[Dict[str, Any]], int, float]]:
        """
        Fetch one task from server - runs in SEPARATE greenlet for isolation.

        Returns:
            Tuple of (task_dict or None, status_code, duration_ms) or None on error

        Why separate greenlet?
        - If HTTP hangs beyond gevent.Timeout, the caller can kill() this greenlet
        - Better isolation than running HTTP directly in _get_task_loop
        - requests.timeout=25 is backup; gevent.Timeout is primary protection
        """
        try:
            params = {
                'agentId': config_dict['agent_id'],
                'agentVersion': __version__
            }
            get_task_server_url = f"{config_dict.get('server_url')}/api/http-teleport/get-task"
            if len(config_dict.get('env_name', '')) > 0:
                params['envName'] = config_dict['env_name']

            logger.debug("Fetching task from %s", get_task_server_url)
            start_time = time.time()

            response: requests.Response = requests.get(
                get_task_server_url,
                headers=_get_headers(),
                timeout=25,  # requests timeout as backup protection
                verify=config_dict.get('verify_cert', False),
                proxies=config_dict['outgoing_proxy'],
                params=params
            )
            duration_ms = (time.time() - start_time) * 1000

            if response.status_code == 200:
                task = response.json().get('data', None)
                return (task, 200, duration_ms)
            elif response.status_code == 204:
                return (None, 204, duration_ms)
            else:
                return (None, response.status_code, duration_ms)

        except requests.exceptions.RequestException as e:
            logger.error("Network error in _fetch_task_once: %s", e)
            return None
        except Exception as e:
            logger.error("Unexpected error in _fetch_task_once: %s", e)
            return None

    def _get_task_loop(self) -> None:
        """
        GetTask greenlet - polls API for tasks.

        ARCHITECTURE: HTTP call is spawned in a SEPARATE greenlet with gevent.Timeout wrapper.
        This provides two layers of protection:
        1. requests.timeout=25 (backup - can be unreliable with SSL/proxy issues)
        2. gevent.Timeout(30) (primary - guaranteed to raise after 30s)

        If HTTP hangs, we can kill() the spawned greenlet without affecting this loop.
        """
        logger.info("GetTask greenlet started")
        thread_backoff_time: int = min_backoff_time
        get_task_timeout: int = 30  # gevent.Timeout wrapper (primary protection)

        while not self._shutdown_event.is_set():
            try:
                logger.info("Requesting task...")
                rate_limiter.throttle()
                health_metrics.record_get_task_call()

                # Spawn HTTP call in SEPARATE greenlet for isolation
                fetch_greenlet = gevent.spawn(self._fetch_task_once)

                try:
                    # Wait for result with timeout - this is the PRIMARY protection
                    with gevent.Timeout(get_task_timeout, exception=GreenletTimeoutError("get_task HTTP timeout")):
                        result = fetch_greenlet.get()

                    # Process result from _fetch_task_once
                    if result is None:
                        # Network or other error - already logged in _fetch_task_once
                        gevent.sleep(5)
                        continue

                    task, status_code, duration_ms = result
                    get_task_server_url = f"{config_dict.get('server_url')}/api/http-teleport/get-task"

                    if status_code == 200:
                        thread_backoff_time = min_backoff_time

                        if task is None:
                            logger.info("Received empty task")
                            _log_get_task_metric(duration_ms, get_task_server_url, 200, task)
                            continue

                        logger.info("Received task: %s", task['taskId'])
                        _log_get_task_metric(duration_ms, get_task_server_url, 200, task)
                        task["version"] = __version__
                        health_metrics.record_task_received(task['taskId'])

                        # Put task in queue for processing
                        self.task_queue.put(task)

                    elif status_code == 204:
                        logger.info("No task available. Waiting...")
                        _log_get_task_metric(duration_ms, get_task_server_url, 204)

                    elif status_code > 500:
                        logger.error("Getting 5XX error %d, increasing backoff time", status_code)
                        _log_get_task_metric(duration_ms, get_task_server_url, status_code)
                        gevent.sleep(thread_backoff_time)
                        thread_backoff_time = min(max_backoff_time, thread_backoff_time * 2)

                    else:
                        logger.error("Unexpected response: %d", status_code)
                        _log_get_task_metric(duration_ms, get_task_server_url, status_code)

                except GreenletTimeoutError:
                    # HTTP call timed out - kill the spawned greenlet and continue
                    logger.warning("get_task HTTP call timed out after %ds, killing greenlet", get_task_timeout)
                    fetch_greenlet.kill()
                    _safe_log_metric('get_task.timeout', 1, {'timeout_sec': str(get_task_timeout)})
                    gevent.sleep(5)

            except Exception as e:
                logger.error("Unexpected error in _get_task_loop: %s", e, exc_info=True)
                gevent.sleep(5)

        logger.info("GetTask greenlet stopped")

    # ==================== QUEUE PROCESSOR GREENLET ====================

    def _queue_processor_loop(self) -> None:
        """Queue processor - takes tasks from queue and spawns workers."""
        logger.info(f"Queue processor greenlet started: id(self)={id(self)}, pool={self.pool}, id(pool)={id(self.pool) if self.pool else 'None'}")

        while not self._shutdown_event.is_set():
            try:
                try:
                    task = self.task_queue.get(timeout=1)
                except gevent.queue.Empty:
                    continue

                if self.pool is None:
                    logger.warning(f"Pool is None: id(self)={id(self)}")
                    logger.warning("Creating new pool...")
                    self._create_pool()
                    if self.pool is None:
                        logger.error("Failed to create pool, re-queuing task")
                        self.task_queue.put(task)
                        gevent.sleep(1)
                        continue

                task_id = task.get('taskId', 'unknown')

                # Use greenlet manager with timeout
                self.greenlet_manager.spawn_with_timeout(
                    pool=self.pool,
                    func=self._process_task_wrapper,
                    timeout=config_dict.get('task_processing_timeout', 3600),  # 1 hour default
                    task_id=task_id,
                    task=task
                )

                logger.debug(f"Spawned worker for task {task_id}")

            except Exception as e:
                logger.error(f"Queue processor error: {e}")
                _safe_log_metric('queue_processor.error', 1, {'error': str(e)})
                gevent.sleep(1)

        logger.info("Queue processor greenlet stopped")

    def _process_task_wrapper(self, task: Dict[str, Any]) -> None:
        """Wrapper for task processing with health tracking."""
        task_id = task.get('taskId', 'unknown')

        health_metrics.increment_active_greenlets()

        try:
            process_task_async(task)
            health_metrics.record_task_completed(success=True)
        except Exception as e:
            logger.error(f"Task {task_id} failed: {e}")
            health_metrics.record_task_completed(success=False)
        finally:
            health_metrics.decrement_active_greenlets()


# ============================================================================
# MAIN ENTRY POINT
# ============================================================================

def main() -> None:
    global config_dict, logger, rate_limiter, metrics_logger, health_metrics

    # Initialize rate limiter
    rate_limiter = RateLimiter(request_limit=25, time_window=15)

    parser = argparse.ArgumentParser(description="Worker1: In-Process Watchdog Solution")
    config_dict, agent_index, debug_mode, enable_stdout_logging = get_initial_config(parser)

    logger = setup_logger(agent_index, debug_mode, enable_stdout_logging)

    # Initialize health metrics with thresholds from config
    health_metrics = HealthMetrics()
    health_metrics.get_task_stale_threshold_sec = config_dict.get('get_task_stale_threshold', 3600)
    health_metrics.task_received_stale_threshold_sec = config_dict.get('task_received_stale_threshold', 43200)

    # Initialize metrics logger ONLY if --metrics flag is passed
    if config_dict.get('metrics_enabled', False):
        metrics_folder = os.path.join(log_folder, 'metrics')
        _createFolder(metrics_folder)
        metrics_file = os.path.join(metrics_folder, f'metrics{agent_index}.json')
        metrics_retention_days = config_dict.get('metrics_retention_days', 7)
        metrics_logger = BufferedMetricsLogger(
            metrics_file,
            flush_interval=10,
            buffer_size=1000,
            backup_count=metrics_retention_days
        )
        logger.info("Metrics logging ENABLED")
    else:
        metrics_logger = None
        logger.info("Metrics logging DISABLED (use --metrics to enable)")

    # Shutdown handler
    watchdog_worker = None

    def shutdown_handler(signum=None, frame=None):
        print_all_thread_stacks()
        logger.info("Shutting down, flushing remaining metrics...")
        if metrics_logger:
            metrics_logger.shutdown()
        if watchdog_worker:
            watchdog_worker.trigger_shutdown()
        logger.info("Metrics flushed and thread stopped. Exiting.")

    atexit.register(shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGUSR1, shutdown_handler)

    logger.info("=" * 60)
    logger.info("Worker1: In-Process Watchdog Solution (Self-Sufficient)")
    logger.info("=" * 60)
    logger.info("Agent Started for url %s, verify %s, timeout %s, outgoing proxy %s, inward %s, uploadToAc %s",
                config_dict.get('server_url'),
                config_dict.get('verify_cert', False), config_dict.get('timeout', 10), config_dict['outgoing_proxy'],
                config_dict['inward_proxy'], config_dict.get('upload_to_ac', None))

    if config_dict['server_url'] is None or config_dict.get('api_key', None) is None:
        logger.error("Empty serverUrl %s", config_dict.get('server_url', True))
        raise ValueError("Server URL and API Key must be provided either as arguments or environment variables")

    # Create and start watchdog worker
    watchdog_worker = WatchdogWorker()

    try:
        watchdog_worker.start()
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")
    except Exception as e:
        logger.error(f"Worker failed: {e}")
        sys.exit(1)

    logger.info("Worker1 exited")


if __name__ == "__main__":
    _clean_temp_output_files()
    _createFolder(armorcode_folder)
    _createFolder(log_folder)
    _createFolder(output_file_folder)
    main()
