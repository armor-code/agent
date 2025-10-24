#!/usr/bin/env python3
from gevent import monkey;

monkey.patch_all()
import gevent
import argparse
import atexit
import base64
import gzip
import json
import logging
import os
import random
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
from typing import Optional, Tuple, Any, Dict, Union, List, Callable
from urllib.parse import urlparse, urlunparse

import gevent
import requests
from gevent.lock import Semaphore
from gevent.pool import Pool

# Global variables
__version__ = "1.1.8"
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

# CRITICAL: Semaphore to limit concurrent teleport endpoint calls
# Shared across all greenlets in the worker to prevent "Too many concurrent requests" errors
# Applies to ALL teleport endpoints: get-task, put-result, upload-logs, upload-result, upload-url
teleport_semaphore: Optional[Semaphore] = None


def main() -> None:
    global config_dict, logger, rate_limiter, metrics_logger, teleport_semaphore

    rate_limiter = RateLimiter(request_limit=25, time_window=15)
    # Initialize semaphore to limit concurrent teleport endpoint calls (max 2)
    teleport_semaphore = Semaphore(2)
    parser = argparse.ArgumentParser()
    config_dict, agent_index, debug_mode = get_initial_config(parser)

    logger = setup_logger(agent_index, debug_mode)

    # Initialize metrics logger
    metrics_folder = os.path.join(armorcode_folder, 'metrics')
    _createFolder(metrics_folder)
    metrics_file = os.path.join(metrics_folder, f'metrics{agent_index}.json')
    metrics_logger = BufferedMetricsLogger(metrics_file, flush_interval=10, buffer_size=1000)

    # Register shutdown handlers to flush metrics
    def shutdown_handler(signum=None, frame=None):
        logger.info("Shutting down, flushing remaining metrics...")
        metrics_logger.shutdown()
        logger.info("Metrics flushed and greenlet stopped. Exiting.")

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


def _get_task_from_server(headers: Dict[str, str], params: Dict[str, str], get_task_server_url: str) -> Tuple[requests.Response, float]:
    """Execute get-task request in a separate greenlet to prevent LoopExit."""
    get_task_start_time = time.time()

    # Acquire semaphore for teleport endpoint call
    with teleport_semaphore:
        get_task_response: requests.Response = requests.get(
            get_task_server_url,
            headers=headers,
            timeout=25, verify=config_dict.get('verify_cert', False),
            proxies=config_dict['outgoing_proxy'],
            params=params
        )

    get_task_duration_ms = (time.time() - get_task_start_time) * 1000
    return get_task_response, get_task_duration_ms


# ============================================================================
# Retry Infrastructure - Smart 429 Handling with Semaphore
# ============================================================================

def is_concurrent_limit_error(response: requests.Response) -> bool:
    """
    Check if 429 response is due to concurrent request limit.

    This distinguishes between:
    - Standard rate limiting (use header delay)
    - Concurrent request limit (use random delay)

    Args:
        response: HTTP response object

    Returns:
        True if response indicates concurrent limit error, False otherwise
    """
    if response.status_code == 429:
        try:
            return "Too many concurrent requests" in response.text
        except Exception:
            # If response.text fails (rare), assume not concurrent error
            return False
    return False


def get_retry_delay(response: requests.Response, default_delay: int = 2) -> float:
    """
    Extract retry delay from response, detecting concurrent errors.

    Priority order:
    1. Check for concurrent error → random delay (0-10s)
    2. Check retry-after header → use header value
    3. Use default delay

    Args:
        response: HTTP response object
        default_delay: Fallback delay in seconds (default: 2)

    Returns:
        Delay in seconds (validated, bounded)
    """
    # Priority 1: Check for concurrent error
    if is_concurrent_limit_error(response):
        delay = random.uniform(0, 10)
        logger.info(f"Concurrent limit error detected, using random delay: {delay:.2f}s")
        return delay

    # Priority 2: Check retry-after header
    retry_after = response.headers.get('X-Rate-Limit-Retry-After-Seconds')

    if retry_after:
        try:
            delay = int(retry_after)

            # Validate: must be positive
            if delay < 0:
                logger.warning(
                    f"Negative retry delay {delay}s in header, using default {default_delay}s"
                )
                return default_delay

            # Validate: cap at 5 minutes
            if delay > 300:
                logger.warning(
                    f"Excessive retry delay {delay}s in header, capping at 300s"
                )
                return 300

            logger.info(f"Using retry-after header delay: {delay}s")
            return delay

        except ValueError:
            logger.warning(
                f"Invalid retry delay '{retry_after}' in header, using default {default_delay}s"
            )

    return default_delay


def retry_on_429(
    func: Callable[[], requests.Response],
    max_retries: int = 5,
    operation_name: str = "request"
) -> Optional[requests.Response]:
    """
    Retry a function on 429 rate limit errors.

    Uses X-Rate-Limit-Retry-After-Seconds header if available,
    or random delay (0-10s) for concurrent errors,
    otherwise uses default 2-second delay.

    CRITICAL: Uses gevent.sleep() not time.sleep() to allow other greenlets
    to run during retry delays, preventing hub from becoming empty.

    Args:
        func: Function to call (must return requests.Response)
        max_retries: Maximum retry attempts (default: 5)
        operation_name: Name for logging

    Returns:
        Response object, or None if unexpected error

    Raises:
        requests.exceptions.RequestException: On network errors
    """
    for attempt in range(max_retries + 1):
        try:
            response = func()

            # Success or non-429 error → return immediately
            if response.status_code != 429:
                return response

            # 429 but retries exhausted → return last response
            if attempt >= max_retries:
                logger.error(
                    f"{operation_name} failed after {max_retries} retries due to rate limiting"
                )
                return response

            # 429 with retries remaining → sleep and retry
            delay = get_retry_delay(response)
            error_type = "concurrent limit" if is_concurrent_limit_error(response) else "rate limit"
            logger.warning(
                f"{operation_name} {error_type} hit "
                f"(attempt {attempt + 1}/{max_retries + 1}), "
                f"retrying in {delay:.2f}s"
            )
            gevent.sleep(delay)
            continue

        except requests.exceptions.RequestException as e:
            logger.error(f"{operation_name} request error: {e}")
            raise

    # Should never reach here
    logger.error(f"{operation_name} unexpected loop exit")
    return None


def process() -> None:
    headers: Dict[str, str] = _get_headers()
    thread_backoff_time: int = min_backoff_time

    # Note: Keepalive greenlet not needed because:
    # 1. Main loop waits with .get(timeout=30) which registers a timer
    # 2. Flush greenlet has gevent.sleep(10) which registers a timer
    # 3. These ensure hub always has pending > 0

    # thread_pool = Pool(config_dict['thread_pool_size'])
    while True:
        try:
            # Get the next task for the agent
            logger.info("Requesting task...")
            rate_limiter.throttle()

            params = {
                'agentId' : config_dict['agent_id']
            }
            get_task_server_url = f"{config_dict.get('server_url')}/api/http-teleport/get-task"
            if len(config_dict.get('env_name', '')) > 0:
                params['envName'] = config_dict['env_name']

            logger.info("Requesting task from %s", get_task_server_url)

            # Spawn get-task in separate greenlet to keep main loop active (prevents LoopExit)
            get_task_greenlet = gevent.spawn(_get_task_from_server, headers, params, get_task_server_url)

            try:
                get_task_response, get_task_duration_ms = get_task_greenlet.get(timeout=30)
            except gevent.Timeout:
                logger.error("Get-task request timed out after 30 seconds")
                gevent.sleep(5)
                continue

            if get_task_response.status_code == 200:
                thread_backoff_time = min_backoff_time
                task: Optional[Dict[str, Any]] = get_task_response.json().get('data', None)

                # Track get-task metric
                metrics_logger.write_metric(
                    "http.request.duration_ms",
                    get_task_duration_ms,
                    tags={
                        "task_id": task.get('taskId', 'none') if task else "none",
                        "operation": "get_task",
                        "url": _get_url_without_params(get_task_server_url),
                        "domain": urlparse(config_dict.get('server_url')).netloc,
                        "method": "GET",
                        "status_code": "200",
                        "has_task": str(task is not None).lower()
                    }
                )

                if task is None:
                    logger.info("Received empty task")
                    gevent.sleep(5)
                    continue

                logger.info("Received task: %s", task['taskId'])
                task["version"] = __version__
                # Process the task
                thread_pool = config_dict.get('thread_pool', None)
                if thread_pool is None:
                    process_task_async(task)
                else:
                    # Use helper greenlet to avoid blocking main loop (prevents LoopExit deadlock)
                    def spawn_when_available(pool, task_to_process):
                        pool.wait_available()
                        pool.spawn(process_task_async, task_to_process)

                    gevent.spawn(spawn_when_available, thread_pool, task)
            elif get_task_response.status_code == 429:
                metrics_logger.write_metric(
                    "http.request.duration_ms",
                    get_task_duration_ms,
                    tags={
                        "task_id": "none",
                        "operation": "get_task",
                        "url": _get_url_without_params(get_task_server_url),
                        "domain": urlparse(config_dict.get('server_url')).netloc,
                        "method": "GET",
                        "status_code": "204",
                        "has_task": "false"
                    }
                )
                logger.info("No task available. Waiting...")
                gevent.sleep(5)
            elif get_task_response.status_code > 500:
                metrics_logger.write_metric(
                    "http.request.duration_ms",
                    get_task_duration_ms,
                    tags={
                        "task_id": "none",
                        "operation": "get_task",
                        "url": _get_url_without_params(get_task_server_url),
                        "domain": urlparse(config_dict.get('server_url')).netloc,
                        "method": "GET",
                        "status_code": str(get_task_response.status_code)
                    }
                )
                logger.error("Getting 5XX error %d, increasing backoff time", get_task_response.status_code)
                gevent.sleep(thread_backoff_time)
                thread_backoff_time = min(max_backoff_time, thread_backoff_time * 2)
            else:
                metrics_logger.write_metric(
                    "http.request.duration_ms",
                    get_task_duration_ms,
                    tags={
                        "task_id": "none",
                        "operation": "get_task",
                        "url": _get_url_without_params(get_task_server_url),
                        "domain": urlparse(config_dict.get('server_url')).netloc,
                        "method": "GET",
                        "status_code": str(get_task_response.status_code)
                    }
                )
                logger.error("Unexpected response: %d", get_task_response.status_code)
                gevent.sleep(5)

        except requests.exceptions.RequestException as e:
            logger.error("Network error: %s", e)
            gevent.sleep(10)  # Wait longer on network errors
        except gevent.hub.LoopExit as e:
            logger.error("Getting LoopExit Error, resetting the thread pool")
            config_dict['thread_pool'] = Pool(config_dict['thread_pool_size'])
        except Exception as e:
            logger.error("Unexpected error while processing: %s", e, exc_info=True)
            gevent.sleep(5)


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
        gevent.sleep(5)


def _log_update_metrics(
    task: Dict[str, Any],
    response: requests.Response,
    duration_ms: float
) -> None:
    """
    Log metrics for update_task operation.

    Separated for testability and reuse across refactored functions.

    Args:
        task: Task dictionary with taskId
        response: HTTP response
        duration_ms: Request duration in milliseconds
    """
    tags = {
        "task_id": task['taskId'],
        "operation": "upload_result",
        "url": _get_url_without_params(
            f"{config_dict.get('server_url')}/api/http-teleport/put-result"
        ),
        "domain": urlparse(config_dict.get('server_url')).netloc,
        "method": "POST",
        "status_code": str(response.status_code),
        "success": str(response.status_code == 200).lower()
    }

    # Add error type for failed requests
    if response.status_code == 429:
        tags["error_type"] = "rate_limit"
    elif response.status_code == 504:
        tags["error_type"] = "timeout"
    elif response.status_code >= 500:
        tags["error_type"] = "server_error"
    elif response.status_code >= 400:
        tags["error_type"] = "client_error"

    metrics_logger.write_metric(
        "http.request.duration_ms",
        duration_ms,
        tags=tags
    )


def update_task(task: Optional[Dict[str, Any]]) -> None:
    """
    Update task result to ArmorCode server.

    Retries on 429 rate limit errors up to 5 times,
    respecting X-Rate-Limit-Retry-After-Seconds header or using
    random delay (0-10s) for concurrent errors.

    Uses global teleport_semaphore to limit concurrent calls.

    Args:
        task: Task dictionary with result data
    """
    if task is None:
        return

    def _make_update_request() -> requests.Response:
        """Inner function for HTTP request with semaphore protection."""
        rate_limiter.throttle()
        update_start_time = time.time()

        # Acquire semaphore for the HTTP call
        with teleport_semaphore:
            response = requests.post(
                f"{config_dict.get('server_url')}/api/http-teleport/put-result",
                headers=_get_headers(),
                json=task,
                timeout=30,
                verify=config_dict.get('verify_cert'),
                proxies=config_dict['outgoing_proxy']
            )

        # Metrics logging happens OUTSIDE semaphore (don't hold it longer than needed)
        update_duration_ms = (time.time() - update_start_time) * 1000
        _log_update_metrics(task, response, update_duration_ms)

        return response

    # Use retry wrapper
    try:
        response = retry_on_429(
            _make_update_request,
            max_retries=5,
            operation_name=f"update_task[{task['taskId']}]"
        )

        # Handle response
        if response and response.status_code == 200:
            logger.info(f"Task {task['taskId']} updated successfully. Response: {response.text}")
        elif response and response.status_code == 504:
            logger.warning(f"Timeout updating task {task['taskId']}: {response.text}")
        elif response and response.status_code == 429:
            logger.warning(f"Rate limit updating task {task['taskId']} after all retries")
        elif response:
            logger.warning(f"Failed to update task {task['taskId']}: {response.text}")

    except requests.exceptions.RequestException as e:
        logger.error(f"Network error updating task {task['taskId']}: {e}")
        # Note: Network errors are propagated from retry_on_429, no need to retry again here


def _get_headers() -> Dict[str, str]:
    headers: Dict[str, str] = {
        "Authorization": f"Bearer {config_dict['api_key']}",
        "Content-Type": "application/json"
    }
    return headers


def check_for_logs_fetch(url, task, temp_output_file_zip):
    """
    Check if this is a logs fetch request and upload logs if so.

    Includes retry on 429 rate limit errors with semaphore protection.

    Args:
        url: Request URL
        task: Task dictionary
        temp_output_file_zip: Temporary file for zipped logs

    Returns:
        True if logs were uploaded, False otherwise
    """
    if 'agent/fetch-logs' in url and 'fetchLogs' in task.get('taskId'):
        try:
            # Zip the logs_folder
            shutil.make_archive(temp_output_file_zip.name[:-4], 'zip', log_folder)

            # Update the task with the zip file information
            task['responseZipped'] = True
            logger.info(f"Logs zipped successfully: {temp_output_file_zip.name}")

            # Prepare upload data
            headers: Dict[str, str] = {
                "Authorization": f"Bearer {config_dict['api_key']}",
            }
            task_json = json.dumps(task)
            files = {
                "file": (temp_output_file_zip.name, open(temp_output_file_zip.name, "rb"), "application/zip"),
                "task": (None, task_json, "application/json")
            }

            upload_logs_url = f"{config_dict.get('server_url')}/api/http-teleport/upload-logs"
            if len(config_dict.get('env_name', '')) > 0:
                upload_logs_url += f"?envName={config_dict.get('env_name')}"

            # Inner function for HTTP call with semaphore protection
            def _upload_logs() -> requests.Response:
                """Inner function for logs upload request."""
                rate_limiter.throttle()

                # Acquire semaphore for teleport endpoint call
                with teleport_semaphore:
                    return requests.post(
                        upload_logs_url,
                        headers=headers,
                        timeout=300,
                        verify=config_dict.get('verify_cert', False),
                        proxies=config_dict['outgoing_proxy'],
                        files=files
                    )

            # Use retry wrapper
            response = retry_on_429(
                _upload_logs,
                max_retries=5,
                operation_name="upload_logs"
            )

            if response and response.status_code == 200:
                logger.info("Logs uploaded successfully")
                return True
            else:
                logger.error(
                    f"Failed to upload logs: code={response.status_code if response else 'None'}, "
                    f"error={response.content if response else 'None'}"
                )
                return True  # Still return True to maintain existing behavior

        except Exception as e:
            logger.error(f"Error zipping logs: {str(e)}")
            raise e
    return False


def process_task(task: Dict[str, Any]) -> Optional[dict[str, Any]]:
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
        metrics_logger.write_metric(
            "http.request.duration_ms",
            http_duration_ms,
            tags={
                "task_id": taskId,
                "operation": "target_request",
                "url": _get_url_without_params(url),
                "domain": urlparse(url).netloc,
                "method": method,
                "status_code": str(response.status_code),
                "success": str(response.status_code < 400).lower()
            }
        )

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
            metrics_logger.write_metric(
                "upload.size_bytes",
                file_size,
                tags={
                    "task_id": taskId,
                    "upload_type": "inline"
                }
            )

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
        metrics_logger.write_metric(
            "task.processing_duration_ms",
            task_total_duration_ms,
            tags={
                "task_id": taskId,
                "method": method,
                "domain": urlparse(url).netloc,
                "http_status": str(task.get('statusCode', 'unknown'))
            }
        )

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
    """
    Upload task response to ArmorCode server.

    Includes retry on 429 rate limit errors with semaphore protection.

    Args:
        temp_file: Temporary file with response
        temp_file_zip: Temporary file for zipped response
        taskId: Task ID
        task: Task dictionary

    Returns:
        None if uploaded to ArmorCode, task dict if using S3
    """
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
                "file": (file_name, open(file_path, "rb"), f"{'application/zip' if success else 'text/plain'}"),
                "task": (None, task_json, "application/json")
            }

            # Inner function for HTTP call with semaphore protection
            def _upload_result_file() -> requests.Response:
                """Inner function for result file upload."""
                rate_limiter.throttle()
                upload_start_time = time.time()

                # Acquire semaphore for teleport endpoint call
                with teleport_semaphore:
                    response = requests.post(
                        f"{config_dict.get('server_url')}/api/http-teleport/upload-result",
                        headers=headers,
                        timeout=300,
                        verify=config_dict.get('verify_cert', False),
                        proxies=config_dict['outgoing_proxy'],
                        files=files
                    )

                # Metrics logging happens OUTSIDE semaphore
                upload_duration_ms = (time.time() - upload_start_time) * 1000

                # Track file upload metrics
                metrics_logger.write_metric(
                    "http.request.duration_ms",
                    upload_duration_ms,
                    tags={
                        "task_id": taskId,
                        "operation": "upload_file",
                        "url": _get_url_without_params(f"{config_dict.get('server_url')}/api/http-teleport/upload-result"),
                        "domain": urlparse(config_dict.get('server_url')).netloc,
                        "method": "POST",
                        "status_code": str(response.status_code),
                        "success": str(response.status_code < 400).lower()
                    }
                )

                # Track upload size
                metrics_logger.write_metric(
                    "upload.size_bytes",
                    file_size,
                    tags={
                        "task_id": taskId,
                        "upload_type": "direct"
                    }
                )

                return response

            # Use retry wrapper
            upload_result = retry_on_429(
                _upload_result_file,
                max_retries=5,
                operation_name=f"upload_result[{taskId}]"
            )

            if upload_result:
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
        self.lock = Semaphore()

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
    """Buffered metrics logger for DataDog. Flushes periodically to preserve timestamps. Uses gevent primitives."""

    def __init__(self, metrics_file: str, flush_interval: int = 10, buffer_size: int = 1000):
        Path(metrics_file).parent.mkdir(parents=True, exist_ok=True)
        self.flush_interval = flush_interval
        self.buffer_size = buffer_size
        self.buffer: List[Dict] = []
        self.buffer_lock = Semaphore()
        self.last_flush_time = time.time()

        self.file_logger = logging.getLogger('metrics_file')
        self.file_logger.setLevel(logging.INFO)
        self.file_logger.propagate = False

        handler = TimedRotatingFileHandler(metrics_file, when="midnight", interval=1, backupCount=7)
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
        while True:
            gevent.sleep(self.flush_interval)
            with self.buffer_lock:
                if self.buffer and (time.time() - self.last_flush_time) >= self.flush_interval:
                    self._flush()

    def flush_now(self):
        """Flush all buffered metrics immediately."""
        with self.buffer_lock:
            self._flush()

    def shutdown(self):
        """Flush remaining metrics and stop the flush greenlet."""
        self.flush_now()
        if self.flush_greenlet and not self.flush_greenlet.dead:
            self.flush_greenlet.kill()


def _get_url_without_params(url: str) -> str:
    """Remove query parameters from URL."""
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, '', '', ''))


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
        print("Output directory already exists: %s", folder_path)


def get_s3_upload_url(taskId: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Get S3 upload URL from ArmorCode server.

    Retries on 429 rate limit errors up to 5 times with semaphore protection.

    Args:
        taskId: Task ID for filename generation

    Returns:
        Tuple of (putUrl, getUrl) or (None, None) on error
    """
    params: Dict[str, str] = {'fileName': f"{taskId}{uuid.uuid4().hex}"}

    def _request_upload_url() -> requests.Response:
        """Inner function for S3 URL request with semaphore protection."""
        rate_limiter.throttle()

        # Acquire semaphore for teleport endpoint call
        with teleport_semaphore:
            return requests.get(
                f"{config_dict.get('server_url')}/api/http-teleport/upload-url",
                params=params,
                headers=_get_headers(),
                timeout=25,
                verify=config_dict.get('verify_cert', False),
                proxies=config_dict['outgoing_proxy']
            )

    try:
        response = retry_on_429(
            _request_upload_url,
            max_retries=5,
            operation_name="get_s3_upload_url"
        )

        if response and response.status_code == 200:
            data: Optional[Dict[str, str]] = response.json().get('data')
            if data:
                return data.get('putUrl'), data.get('getUrl')
            logger.warning("No data in S3 upload URL response")
        else:
            logger.warning(
                f"Failed to get S3 URL: {response.status_code if response else 'None'}"
            )

        return None, None

    except requests.exceptions.RequestException as e:
        logger.error(f"Network error getting S3 upload URL: {e}")
    except Exception as e:
        logger.exception(f"Unexpected error getting S3 upload URL: {e}")

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
