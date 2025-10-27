#!/usr/bin/env python3
from gevent import monkey;

monkey.patch_all()
from gevent import Timeout
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

# Limit concurrent teleport calls (max 2 per worker)
teleport_semaphore: Optional[Semaphore] = None

# Timeout for teleport operations (prevents semaphore deadlock if operation hangs)
TELEPORT_TIMEOUT = int(os.getenv('TELEPORT_TIMEOUT_SECONDS', '60'))


def main() -> None:
    global config_dict, logger, rate_limiter, teleport_semaphore

    rate_limiter = RateLimiter(request_limit=25, time_window=15)
    teleport_semaphore = Semaphore(2)  # Max 2 concurrent teleport calls
    parser = argparse.ArgumentParser()
    config_dict, agent_index, debug_mode = get_initial_config(parser)

    logger = setup_logger(agent_index, debug_mode)

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

    try:
        with Timeout(TELEPORT_TIMEOUT):
            with teleport_semaphore:
                get_task_response: requests.Response = requests.get(
                    get_task_server_url,
                    headers=headers,
                    timeout=25, verify=config_dict.get('verify_cert', False),
                    proxies=config_dict['outgoing_proxy'],
                    params=params
                )
    except Timeout:
        logger.error(f"Get-task timed out after {TELEPORT_TIMEOUT}s, semaphore released")
        raise

    get_task_duration_ms = (time.time() - get_task_start_time) * 1000
    return get_task_response, get_task_duration_ms


def is_concurrent_limit_error(response: requests.Response) -> bool:
    """Check if 429 is due to concurrent request limit vs rate limit."""
    if response.status_code == 429:
        try:
            return "Too many concurrent requests" in response.text
        except Exception:
            # If response.text fails (rare), assume not concurrent error
            return False
    return False


def get_retry_delay(response: requests.Response, default_delay: int = 2) -> float:
    """Get retry delay: concurrent error (0-10s) > header > default."""
    if is_concurrent_limit_error(response):
        delay = random.uniform(0, 10)
        logger.info(f"Concurrent limit error, random delay: {delay:.2f}s")
        return delay

    retry_after = response.headers.get('X-Rate-Limit-Retry-After-Seconds')
    if retry_after:
        try:
            delay = int(retry_after)
            if delay < 0:
                logger.warning(f"Negative retry delay {delay}s, using default {default_delay}s")
                return default_delay
            if delay > 60:
                logger.warning(f"Excessive retry delay {delay}s, capping at 60s")
                return 60
            logger.info(f"Using header delay: {delay}s")
            return delay
        except ValueError:
            logger.warning(f"Invalid retry delay '{retry_after}', using default {default_delay}s")

    return default_delay


def retry_request(
    func: Callable[[], requests.Response],
    max_retries: int = 5,
    operation_name: str = "request"
) -> Optional[requests.Response]:
    """Retry on 429 (rate limit) or 504 (timeout). 429 uses smart delay, 504 uses exponential backoff."""
    for attempt in range(max_retries + 1):
        try:
            response = func()

            if response.status_code not in (429, 504):
                return response

            if attempt >= max_retries:
                logger.error(f"{operation_name} failed after {max_retries} retries")
                return response

            # Calculate delay based on error type
            if response.status_code == 429:
                delay = get_retry_delay(response)
                error_type = "concurrent limit" if is_concurrent_limit_error(response) else "rate limit"
            else:  # 504
                delay = min(1 * (2 ** attempt), 30)  # Exponential: 1s, 2s, 4s, 8s, 16s, 30s
                error_type = "gateway timeout"

            logger.warning(f"{operation_name} {error_type} (attempt {attempt + 1}/{max_retries + 1}), retry in {delay:.2f}s")
            gevent.sleep(delay)

        except requests.exceptions.RequestException as e:
            logger.error(f"{operation_name} request error: {e}")
            raise

    logger.error(f"{operation_name} unexpected loop exit")
    return None


def delayed_retry(delay_seconds: int) -> None:
    """Wait by spawning timer greenlet. Keeps hub alive during main loop delays."""
    timer = gevent.spawn(lambda: gevent.sleep(delay_seconds))
    timer.join()  # Wait for timer, but timer greenlet keeps hub active


def process() -> None:
    headers: Dict[str, str] = _get_headers()
    thread_backoff_time: int = min_backoff_time

    while True:
        try:
            # Get the next task for the agent
            logger.info("Requesting task...")
            rate_limiter.throttle()

            params = {
                'agentId': config_dict['agent_id'],
                'agentVersion': __version__
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
                delayed_retry(5)
                continue

            if get_task_response.status_code == 200:
                thread_backoff_time = min_backoff_time
                task: Optional[Dict[str, Any]] = get_task_response.json().get('data', None)

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
                    thread_pool.wait_available()
                    thread_pool.spawn(process_task_async, task)
            elif get_task_response.status_code == 429:
                logger.info("No task available. Waiting...")
                delayed_retry(5)
            elif get_task_response.status_code > 500:
                logger.error("Getting 5XX error %d, increasing backoff time", get_task_response.status_code)
                delayed_retry(thread_backoff_time)
                thread_backoff_time = min(max_backoff_time, thread_backoff_time * 2)
            else:
                logger.error("Unexpected response: %d", get_task_response.status_code)
                delayed_retry(5)

        except requests.exceptions.RequestException as e:
            logger.error("Network error: %s", e)
            delayed_retry(10)  # Wait longer on network errors
        except gevent.hub.LoopExit as e:
            logger.error("Getting LoopExit Error, resetting the thread pool")
            config_dict['thread_pool'] = Pool(config_dict['thread_pool_size'])
        except Exception as e:
            logger.error("Unexpected error while processing: %s", e, exc_info=True)
            delayed_retry(5)


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


def update_task(task: Optional[Dict[str, Any]]) -> None:
    """Update task result with 429 retry and semaphore protection."""
    if task is None:
        return

    def _make_update_request() -> requests.Response:
        rate_limiter.throttle()
        try:
            with Timeout(TELEPORT_TIMEOUT):
                with teleport_semaphore:
                    response = requests.post(
                        f"{config_dict.get('server_url')}/api/http-teleport/put-result",
                        headers=_get_headers(),
                        json=task,
                        timeout=30,
                        verify=config_dict.get('verify_cert'),
                        proxies=config_dict['outgoing_proxy']
                    )
            return response
        except Timeout:
            logger.error(f"Put-result timed out after {TELEPORT_TIMEOUT}s, semaphore released")
            raise

    try:
        response = retry_request(_make_update_request, max_retries=5, operation_name=f"update_task[{task['taskId']}]")

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


def _get_headers() -> Dict[str, str]:
    headers: Dict[str, str] = {
        "Authorization": f"Bearer {config_dict['api_key']}",
        "Content-Type": "application/json"
    }
    return headers


def check_for_logs_fetch(url, task, temp_output_file_zip):
    """Upload agent logs if this is a fetch-logs request."""
    if 'agent/fetch-logs' in url and 'fetchLogs' in task.get('taskId'):
        try:
            shutil.make_archive(temp_output_file_zip.name[:-4], 'zip', log_folder)
            task['responseZipped'] = True
            logger.info(f"Logs zipped successfully: {temp_output_file_zip.name}")

            headers: Dict[str, str] = {"Authorization": f"Bearer {config_dict['api_key']}"}
            task_json = json.dumps(task)
            files = {
                "file": (temp_output_file_zip.name, open(temp_output_file_zip.name, "rb"), "application/zip"),
                "task": (None, task_json, "application/json")
            }

            upload_logs_url = f"{config_dict.get('server_url')}/api/http-teleport/upload-logs"
            if len(config_dict.get('env_name', '')) > 0:
                upload_logs_url += f"?envName={config_dict.get('env_name')}"

            def _upload_logs() -> requests.Response:
                rate_limiter.throttle()
                try:
                    with Timeout(TELEPORT_TIMEOUT):
                        with teleport_semaphore:
                            return requests.post(
                                upload_logs_url,
                                headers=headers,
                                timeout=300,
                                verify=config_dict.get('verify_cert', False),
                                proxies=config_dict['outgoing_proxy'],
                                files=files
                            )
                except Timeout:
                    logger.error(f"Upload-logs timed out after {TELEPORT_TIMEOUT}s, semaphore released")
                    raise

            response = retry_request(_upload_logs, max_retries=5, operation_name="upload_logs")

            if response and response.status_code == 200:
                logger.info("Logs uploaded successfully")
                return True
            else:
                logger.error(f"Failed to upload logs: code={response.status_code if response else 'None'}, error={response.content if response else 'None'}")
                return True

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
    """Upload task response with 429 retry and semaphore protection."""
    if config_dict.get('upload_to_ac', True):
        try:
            success = zip_response(temp_file, temp_file_zip)
            file_path = temp_file_zip if success else temp_file
            task['responseZipped'] = success
            file_name = f"{taskId}_{uuid.uuid4().hex}.{'zip' if success else 'txt'}"

            headers: Dict[str, str] = {"Authorization": f"Bearer {config_dict['api_key']}"}
            task_json = json.dumps(task)
            files = {
                "file": (file_name, open(file_path, "rb"), f"{'application/zip' if success else 'text/plain'}"),
                "task": (None, task_json, "application/json")
            }

            def _upload_result_file() -> requests.Response:
                rate_limiter.throttle()
                try:
                    with Timeout(TELEPORT_TIMEOUT):
                        with teleport_semaphore:
                            response = requests.post(
                                f"{config_dict.get('server_url')}/api/http-teleport/upload-result",
                                headers=headers,
                                timeout=300,
                                verify=config_dict.get('verify_cert', False),
                                proxies=config_dict['outgoing_proxy'],
                                files=files
                            )
                    return response
                except Timeout:
                    logger.error(f"Upload-result timed out after {TELEPORT_TIMEOUT}s, semaphore released")
                    raise

            upload_result = retry_request(_upload_result_file, max_retries=5, operation_name=f"upload_result[{taskId}]")

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
    """Get S3 upload URL with 429 retry and semaphore protection."""
    params: Dict[str, str] = {'fileName': f"{taskId}{uuid.uuid4().hex}"}

    def _request_upload_url() -> requests.Response:
        rate_limiter.throttle()
        try:
            with Timeout(TELEPORT_TIMEOUT):
                with teleport_semaphore:
                    return requests.get(
                        f"{config_dict.get('server_url')}/api/http-teleport/upload-url",
                        params=params,
                        headers=_get_headers(),
                        timeout=25,
                        verify=config_dict.get('verify_cert', False),
                        proxies=config_dict['outgoing_proxy']
                    )
        except Timeout:
            logger.error(f"Get-s3-upload-url timed out after {TELEPORT_TIMEOUT}s, semaphore released")
            raise

    try:
        response = retry_request(_request_upload_url, max_retries=5, operation_name="get_s3_upload_url")

        if response and response.status_code == 200:
            data: Optional[Dict[str, str]] = response.json().get('data')
            if data:
                return data.get('putUrl'), data.get('getUrl')
            logger.warning("No data in S3 upload URL response")
        else:
            logger.warning(f"Failed to get S3 URL: {response.status_code if response else 'None'}")

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
    if global_config.get("threadPoolSize", 25):
        config_dict['thread_pool_size'] = global_config.get("threadPoolSize", 25)
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
        "thread_pool_size": 25  # Connection thread_pool size
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
    parser.add_argument("--poolSize", required=False, help="Multi threading thread_pool size", default=25)
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
    config['thread_pool'] = Pool(config.get('thread_pool_size', 25))
    return config, agent_index, debug_mode


if __name__ == "__main__":
    _clean_temp_output_files()
    _createFolder(armorcode_folder)  # create parent level folder for logs anf files
    _createFolder(log_folder)  # create folder to store log files
    _createFolder(output_file_folder)  # create folder to store output files
    main()
