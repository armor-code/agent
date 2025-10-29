#!/usr/bin/env python3


from gevent import monkey;
monkey.patch_all()
import gevent
import threading
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
from collections import deque
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Optional, Tuple, Any, Dict, Union, List
from urllib.parse import urlparse, urlunparse

import requests
from gevent.pool import Pool

# Global variables
__version__ = "1.1.10"
letters: str = string.ascii_letters
rand_string: str = ''.join(secrets.choice(letters) for _ in range(10))

ac_str = 'armorcode'

armorcode_folder: str = os.path.join(tempfile.gettempdir(), ac_str)
log_folder: str = os.path.join(armorcode_folder, 'log')
output_file_folder: str = os.path.join(armorcode_folder, 'output_files')

max_file_size: int = 1024 * 500  # max_size data that would be sent in payload, more than that will send via s3
logger: Optional[logging.Logger] = None

max_retry: int = 3
max_backoff_time: int = 600
min_backoff_time: int = 5

# throttling to 25 requests per seconds to avoid rate limit errors
rate_limiter = None
config_dict: dict = None
metrics_logger = None


def main() -> None:
    global config_dict, logger, rate_limiter, metrics_logger

    # Instantiate RateLimiter for 25 requests per 15 seconds window
    rate_limiter = RateLimiter(request_limit=25, time_window=15)
    parser = argparse.ArgumentParser()
    config_dict, agent_index, debug_mode = get_initial_config(parser)

    logger = setup_logger(agent_index, debug_mode)

    # Initialize metrics logger
    metrics_folder = os.path.join(log_folder, 'metrics')
    _createFolder(metrics_folder)
    metrics_file = os.path.join(metrics_folder, f'metrics{agent_index}.json')
    metrics_retention_days = config_dict.get('metrics_retention_days', 7)
    metrics_logger = BufferedMetricsLogger(metrics_file, flush_interval=10, buffer_size=1000, backup_count=metrics_retention_days)

    # Register shutdown handlers to flush metrics
    def shutdown_handler(signum=None, frame=None):
        logger.info("Shutting down, flushing remaining metrics...")
        metrics_logger.shutdown()
        logger.info("Metrics flushed and thread stopped. Exiting.")

    atexit.register(shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)

    logger.info("Agent Started for url %s, verify %s, timeout %s, outgoing proxy %s, inward %s, uploadToAc %s",
                config_dict.get('server_url'),
                config_dict.get('verify_cert', False), config_dict.get('timeout', 10), config_dict['outgoing_proxy'],
                config_dict['inward_proxy'], config_dict.get('upload_to_ac', None))

    if config_dict['server_url'] is None or config_dict.get('api_key', None) is None:
        logger.error("Empty serverUrl %s", config_dict.get('server_url', True))
        raise ValueError("Server URL and API Key must be provided either as arguments or environment variables")

    process()

def get_pool_status(pool):
    """Get comprehensive status of a ThreadPool"""
    free = pool._free_count()
    return {
        'max_size': pool.maxsize,
        'current_threads': pool.size,
        'idle_threads': free,
        'busy_threads': pool.size - free,
        'queued_tasks': pool._queue.qsize()
    }

def process() -> None:
    headers: Dict[str, str] = _get_headers()
    thread_backoff_time: int = min_backoff_time
    # thread_pool = Pool(config_dict['thread_pool_size'])
    while True:
        try:
            # Get the next task for the agent
            logger.info("Requesting task...")
            rate_limiter.throttle()

            params = {
                'agentId' : config_dict['agent_id'],
                'agentVersion': __version__
            }
            get_task_server_url = f"{config_dict.get('server_url')}/api/http-teleport/get-task"
            if len(config_dict.get('env_name', '')) > 0:
                params['envName'] = config_dict['env_name']

            logger.info("Requesting task from %s", get_task_server_url)
            get_task_start_time = time.time()
            get_task_response: requests.Response = requests.get(
                get_task_server_url,
                headers=headers,
                timeout=25, verify=config_dict.get('verify_cert', False),
                proxies=config_dict['outgoing_proxy'],
                params=params
            )
            get_task_duration_ms = (time.time() - get_task_start_time) * 1000

            if get_task_response.status_code == 200:
                thread_backoff_time = min_backoff_time
                task: Optional[Dict[str, Any]] = get_task_response.json().get('data', None)

                # Track get-task metric
                _log_get_task_metric(get_task_duration_ms, get_task_server_url, 200, task)

                if task is None:
                    logger.info("Received empty task")
                    continue

                logger.info("Received task: %s", task['taskId'])
                task["version"] = __version__
                # Process the task
                thread_pool = config_dict.get('thread_pool', None)
                if thread_pool is None:
                    process_task_async(task)
                else:
                    try:
                        thread_pool.wait_available()  # Wait if the thread_pool is full
                    except Exception as e:
                        logger.error("Error while getting new thread status of thread pool ", e, exc_info=True)
                        config_dict['thread_pool'] = Pool(config_dict.get('thread_pool_size', 5))
                        thread_pool = config_dict['thread_pool']

                    thread_pool.spawn(process_task_async, task)  # Submit the task when free
            elif get_task_response.status_code == 204:
                _log_get_task_metric(get_task_duration_ms, get_task_server_url, 204)
                logger.info("No task available. Waiting...")
            elif get_task_response.status_code > 500:
                _log_get_task_metric(get_task_duration_ms, get_task_server_url, get_task_response.status_code)
                logger.error("Getting 5XX error %d, increasing backoff time", get_task_response.status_code)
                gevent.sleep(thread_backoff_time)
                thread_backoff_time = min(max_backoff_time, thread_backoff_time * 2)
            else:
                _log_get_task_metric(get_task_duration_ms, get_task_server_url, get_task_response.status_code)
                logger.error("Unexpected response: %d", get_task_response.status_code)

        except requests.exceptions.RequestException as e:
            logger.error("Network error: %s", e)
        except Exception as e:
            logger.error("Unexpected error while processing: %s", e, exc_info=True)

def process_task_async(task: Dict[str, Any]) -> None:
    url: str = task.get('url')
    taskId: str = task.get('taskId')
    method: str = task.get('method').upper()

    try:
        result: Dict[str, Any] = process_task(task)
        # Update the task status
        update_task(result)
    except Exception as e:
        logger.info("Unexpected error while processing task id: %s,  method: %s url: %s, error: %s", taskId, method,
                    url, e)


def _log_update_metrics(
    task: Dict[str, Any],
    response: requests.Response,
    duration_ms: float
) -> None:
    """
    Log metrics for update_task operation.

    Args:
        task: Task dictionary with taskId
        response: HTTP response
        duration_ms: Request duration in milliseconds
    """
    try:
        # Build base tags
        tags = _build_http_request_tags(
            task_id=task.get('taskId', 'unknown'),
            operation="upload_result",
            url=f"{config_dict.get('server_url')}/api/http-teleport/put-result",
            method="POST",
            status_code=response.status_code,
            success=str(response.status_code == 200).lower()
        )

        # Add error type for failed requests
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
    if task is None:
        return
    # Update the task status
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

        # Log metrics for update operation
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


def _get_headers() -> Dict[str, str]:
    headers: Dict[str, str] = {
        "Authorization": f"Bearer {config_dict['api_key']}",
        "Content-Type": "application/json"
    }
    return headers


def check_for_logs_fetch(url, task, temp_output_file_zip):
    if 'agent/fetch-logs' in url and 'fetchLogs' in task.get('taskId'):
        try:

            # Zip the logs_folder
            shutil.make_archive(temp_output_file_zip.name[:-4], 'zip', log_folder)

            # Update the task with the zip file information
            task['responseZipped'] = True
            headers: Dict[str, str] = {
                "Authorization": f"Bearer {config_dict['api_key']}",
            }
            logger.info(f"Logs zipped successfully: {temp_output_file_zip.name}")
            task_json = json.dumps(task)
            files = {
                # 'fileFieldName' is the name of the form field expected by the server
                "file": (temp_output_file_zip.name, open(temp_output_file_zip.name, "rb"), f"{'application/zip'}"),
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
                logger.error("Response code while uploading is not 200 , response code {} and error {} ", upload_result.status_code, upload_result.content)
            return True
        except Exception as e:
            logger.error(f"Error zipping logs: {str(e)}")
            raise e
    return False


def process_task(task: Dict[str, Any]) -> Dict[str, Any]:
    url: str = task.get('url')
    input_data: Any = task.get('input')
    taskId: str = task.get('taskId')
    headers: Dict[str, str] = task.get('requestHeaders', {})
    method: str = task.get('method').upper()
    expiryTime: int = task.get('expiryTsMs', round((time.time() + 300) * 1000))
    logger.info("Processing task %s: %s %s", taskId, method, url)

    task_start_time = time.time()

    # creating temp file to store outputs
    _createFolder(log_folder)  # create folder to store log files
    _createFolder(output_file_folder)  # create folder to store output files
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
        # Running the request
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
            logger.debug("Input data is str")
            encoded_input_data = input_data.encode('utf-8')
        elif isinstance(input_data, bytes):
            logger.debug("Input data is bytes and already encoded")
            encoded_input_data = input_data
        else:
            logger.debug("Input data is not str or bytes %s", input_data)


        http_start_time = time.time()
        response: requests.Response = requests.request(method, url, headers=headers, data=encoded_input_data, stream=True,
                                                       timeout=(15, timeout), verify=config_dict.get('verify_cert'),
                                                       proxies=config_dict['inward_proxy'])
        http_duration_ms = (time.time() - http_start_time) * 1000
        logger.info("Response: %d", response.status_code)

        # Track HTTP request to target URL
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
            # Check if the response is chunked
            is_chunked: bool = response.headers.get('Transfer-Encoding', None) == 'chunked'

            if is_chunked:
                logger.info("Processing in chunks...")
                # Process the response in chunks
                with open(temp_output_file.name, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=1024 * 100):
                        if chunk:
                            f.write(chunk)
            else:
                logger.info("Non-chunked response, processing whole payload...")
                ##data = response.content  # Entire response is downloaded
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
        is_s3_upload: bool = file_size > max_file_size  # if size is greater than max_size, upload data to s3

        if not is_s3_upload:
            logger.info("Data is less than %s, sending data in response", max_file_size)
            with open(temp_output_file.name, 'rb') as file:
                file_data = file.read()
                if len(file_data) == 0:
                    return task
                base64_string = base64.b64encode(file_data).decode('utf-8')
                task['responseBase64'] = True
                task['output'] = base64_string

            # Track inline upload size
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
        # Track overall task processing duration
        task_total_duration_ms = (time.time() - task_start_time) * 1000
        tags = _build_task_processing_tags(taskId, method, url, task.get('statusCode', 'unknown'))
        _safe_log_metric("task.processing_duration_ms", task_total_duration_ms, tags)

        temp_output_file.close()
        temp_output_file_zip.close()
        os.unlink(temp_output_file.name)
        os.unlink(temp_output_file_zip.name)
    return task


def zip_response(temp_file, temp_file_zip) -> bool:
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


def upload_response(temp_file, temp_file_zip, taskId: str, task: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if config_dict.get('upload_to_ac', True):
        try:
            success = zip_response(temp_file, temp_file_zip)
            file_path = temp_file_zip if success else temp_file
            task['responseZipped'] = success
            file_name = f"{taskId}_{uuid.uuid4().hex}.{'zip' if success else 'txt'}"
            file_size = os.path.getsize(file_path)
            headers: Dict[str, str] = {
                "Authorization": f"Bearer {config_dict['api_key']}",
            }
            task_json = json.dumps(task)
            files = {
                # 'fileFieldName' is the name of the form field expected by the server
                "file": (file_name, open(file_path, "rb"), f"{'application/zip' if success else 'text/plain'}"),
                "task": (None, task_json, "application/json")
                # If you have multiple files, you can add them here as more entries
            }
            rate_limiter.throttle()
            upload_start_time = time.time()
            upload_result: requests.Response = requests.post(
                f"{config_dict.get('server_url')}/api/http-teleport/upload-result",
                headers=headers,
                timeout=300, verify=config_dict.get('verify_cert', False), proxies=config_dict['outgoing_proxy'],
                files=files
            )
            upload_duration_ms = (time.time() - upload_start_time) * 1000

            # Track file upload metrics
            tags = _build_http_request_tags(
                task_id=taskId,
                operation="upload_file",
                url=f"{config_dict.get('server_url')}/api/http-teleport/upload-result",
                method="POST",
                status_code=str(upload_result.status_code),
                success=str(upload_result.status_code < 400).lower()
            )
            _safe_log_metric("http.request.duration_ms", upload_duration_ms, tags)

            # Track upload size
            tags = _build_upload_tags(taskId, "direct")
            _safe_log_metric("upload.size_bytes", file_size, tags)

            logger.info("Upload result response: %s, code: %d", upload_result.text, upload_result.status_code)
            upload_result.raise_for_status()
            return None
        except Exception as e:
            logger.error("Unable to upload file to armorcode: %s", e)
            raise e
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


def check_and_update_encode_url(headers, url: str):
    if "/cxrestapi/auth/identity/connect/token" in url:
        headers["Content-Type"] = "application/x-www-form-urlencoded"


class RateLimiter:
    def __init__(self, request_limit: int, time_window: int) -> None:
        self.request_limit = request_limit
        self.time_window = time_window
        self.timestamps = deque()
        self.lock = threading.Lock()

    def set_limits(self, request_limit: int, time_window: int):
        self.request_limit = request_limit
        self.time_window = time_window

    def allow_request(self) -> bool:
        with self.lock:
            current_time = time.time()

            # Remove timestamps older than the time window
            while self.timestamps and self.timestamps[0] < current_time - self.time_window:
                self.timestamps.popleft()

            # Check if we can send a new request
            if len(self.timestamps) < self.request_limit:
                self.timestamps.append(current_time)
                return True
            return False

    def throttle(self) -> None:
        while not self.allow_request():
            gevent.sleep(0.5)


class BufferedMetricsLogger:
    """Buffered metrics logger for DataDog. Flushes periodically to preserve timestamps. Uses threading primitives."""

    def __init__(self, metrics_file: str, flush_interval: int = 10, buffer_size: int = 1000, backup_count: int = 7):
        Path(metrics_file).parent.mkdir(parents=True, exist_ok=True)
        self.flush_interval = flush_interval
        self.buffer_size = buffer_size
        self.backup_count = backup_count
        self.buffer: List[Dict] = []
        self.buffer_lock = threading.Lock()
        self.last_flush_time = time.time()
        self.shutdown_flag = threading.Event()

        self.file_logger = logging.getLogger('metrics_file')
        self.file_logger.setLevel(logging.INFO)
        self.file_logger.propagate = False

        handler = TimedRotatingFileHandler(metrics_file, when="midnight", interval=1, backupCount=backup_count)
        handler.setFormatter(logging.Formatter('%(message)s'))
        self.file_logger.addHandler(handler)

        self.flush_thread = threading.Thread(target=self._auto_flush_loop, daemon=True)
        self.flush_thread.start()

    def write_metric(self, metric_name: str, value: float, tags: Dict[str, str] = None):
        timestamp_ms = int(time.time() * 1000)
        metric_event = {
            "@timestamp": timestamp_ms,
            "metric_name": metric_name,
            "value": value,
            "tags": tags or {}
        }
        with self.buffer_lock:
            self.buffer.append(metric_event)
            if len(self.buffer) >= self.buffer_size:
                self._flush()

    def _flush(self):
        if not self.buffer:
            return
        for event in self.buffer:
            self.file_logger.info(json.dumps(event))
        self.buffer.clear()
        self.last_flush_time = time.time()

    def _auto_flush_loop(self):
        while not self.shutdown_flag.is_set():
            time.sleep(self.flush_interval)
            with self.buffer_lock:
                if self.buffer and (time.time() - self.last_flush_time) >= self.flush_interval:
                    self._flush()

    def flush_now(self):
        """Flush all buffered metrics immediately."""
        with self.buffer_lock:
            self._flush()

    def shutdown(self):
        """Flush remaining metrics and stop the flush thread."""
        self.flush_now()
        self.shutdown_flag.set()
        if self.flush_thread.is_alive():
            self.flush_thread.join(timeout=5)


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
        # Add any extra tags
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


def upload_s3(temp_file, preSignedUrl: str, headers: Dict[str, Any]) -> bool:
    headersForS3: Dict[str, str] = {}
    if 'Content-Encoding' in headers and headers['Content-Encoding'] is not None:
        headersForS3['Content-Encoding'] = headers['Content-Encoding']
    if 'Content-Type' in headers and headers['Content-Type'] is not None:
        headersForS3['Content-Type'] = headers['Content-Type']

    try:
        with open(temp_file, 'rb') as file:
            response: requests.Response = requests.put(preSignedUrl, headers=headersForS3, data=file,
                                                       verify=config_dict.get('verify_cert', False),
                                                       proxies=config_dict['outgoing_proxy'], timeout=120)
            response.raise_for_status()
            logger.info('File uploaded successfully to S3')
            return True
    except requests.exceptions.RequestException as e:
        logger.error("Network error uploading to S3: %s", e)
        raise
    except Exception as e:
        logger.exception("Unexpected error uploading to S3: %s", e)
        raise


def _createFolder(folder_path: str) -> None:
    if not os.path.exists(folder_path):  # Check if the directory exists
        try:
            os.mkdir(folder_path)  # Create the directory if it doesn't exist
            print("Created output directory: %s", folder_path)
        except Exception as e:
            print("Error creating output folder: %s", folder_path)
    else:
        return


def get_s3_upload_url(taskId: str) -> Tuple[Optional[str], Optional[str]]:
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


# Function to set up logging with timed rotation and log retention
def setup_logger(index: str, debug_mode: bool) -> logging.Logger:
    log_filename: str = os.path.join(log_folder, f"app_log{index}.log")

    # Create a TimedRotatingFileHandler
    handler: TimedRotatingFileHandler = TimedRotatingFileHandler(
        log_filename, when="midnight", interval=1, backupCount=7
    )  # This will keep logs for the last 7 days

    # Set the log format
    formatter: logging.Formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    handler.setFormatter(formatter)

    # Create the logger instance
    logger: logging.Logger = logging.getLogger(__name__)
    if debug_mode:
        logger.setLevel(logging.DEBUG)
    else:
        logger.setLevel(logging.INFO)  # Set the log level (DEBUG, INFO, etc.)

    logger.addHandler(handler)
    logger.info("Log folder is created %s", log_folder)
    return logger


def str2bool(v):
    if isinstance(v, bool):
        return v
    if v is None:
        return True  # If no value is provided, default to True
    if v.lower() in ('yes', 'true', 't', '1'):
        return True
    elif v.lower() in ('no', 'false', 'f', '0'):
        return False
    else:
        raise argparse.ArgumentTypeError('Boolean value expected.')


def _clean_temp_output_files() -> None:
    if os.path.exists(output_file_folder):
        try:
            ## delete all files in this folder
            for file in os.listdir(output_file_folder):
                file_path = os.path.join(output_file_folder, file)
                if os.path.isfile(file_path):
                    os.remove(file_path)
        except Exception as e:
            print("Error cleaning temp output files")

def update_agent_config(global_config: dict[str, Any]) -> None:
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
    return

def generate_unique_id():
    # Get current timestamp (Unix time in seconds)
    timestamp = int(time.time())

    # Generate a random 6-character hex value
    random_hex = uuid.uuid4().hex[:6]

    # Combine timestamp and hex
    unique_id = f"{timestamp}_{random_hex}"

    return unique_id



def get_initial_config(parser) -> tuple[dict[str, Union[Union[bool, None, str, int], Any]], str, bool]:
    global rate_limiter
    config = {
        "api_key": None,  # Optional[str]
        "server_url": None,  # Optional[str]           # Default logger (None)
        "verify_cert": True,  # Whether to verify SSL certificates
        "timeout": 10,  # Request timeout in seconds     # Throttling (e.g., 25 requests per second)
        "inward_proxy": None,  # Proxy for incoming requests
        "outgoing_proxy": None,  # Proxy for outgoing requests (e.g., to ArmorCode)
        "upload_to_ac": False,  # Whether to upload to ArmorCode
        "env_name": None,  # Environment name (Optional[str])
        "thread_pool_size": 5  # Connection thread_pool size
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
    parser.add_argument("--poolSize", required=False, help="Multi threading thread_pool size", default=5)
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

    args = parser.parse_args()
    config['agent_id']  = generate_unique_id()
    config['server_url'] = args.serverUrl
    config['api_key'] = args.apiKey
    agent_index: str = args.index
    timeout_cmd = args.timeout
    pool_size_cmd = args.poolSize
    verify_cmd = args.verify
    debug_cmd = args.debugMode
    rate_limit_per_min = args.rateLimitPerMin
    config['metrics_retention_days'] = args.metricsRetentionDays

    config['upload_to_ac'] = args.uploadToAc

    rate_limiter.set_limits(rate_limit_per_min, 60)
    inward_proxy_https = args.inwardProxyHttps
    inward_proxy_http = args.inwardProxyHttp

    outgoing_proxy_https = args.outgoingProxyHttps
    outgoing_proxy_http = args.outgoingProxyHttp
    config['env_name'] = args.envName

    if inward_proxy_https is None and inward_proxy_http is None:
        config['inward_proxy'] = None
    else:
        inward_proxy = {}
        if inward_proxy_https is not None:
            inward_proxy['https'] = inward_proxy_https
        if inward_proxy_http is not None:
            inward_proxy['http'] = inward_proxy_http
        config['inward_proxy'] = inward_proxy

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
    return config, agent_index, debug_mode


if __name__ == "__main__":
    _clean_temp_output_files()
    _createFolder(armorcode_folder)  # create parent level folder for logs anf files
    _createFolder(log_folder)  # create folder to store log files
    _createFolder(output_file_folder)  # create folder to store output files
    main()
