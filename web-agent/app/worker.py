#!/usr/bin/env python3
import argparse
import base64
import json
import os
import secrets
import string
import uuid
from collections import deque
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Optional, Tuple, Any, Dict

import requests
import logging
import time
import gzip
from urllib.parse import unquote
import tempfile

# Global variables
__version__ = "1.1.2"
letters: str = string.ascii_letters
rand_string: str = ''.join(secrets.choice(letters) for _ in range(10))

ac_str = 'armorcode'

armorcode_folder: str = os.path.join(tempfile.gettempdir(), ac_str)
log_folder: str = os.path.join(armorcode_folder, 'log')
output_file_folder: str = os.path.join(armorcode_folder, 'output_files')

max_file_size: int = 1024 * 500  # max_size data that would be sent in payload, more than that will send via s3
logger: Optional[logging.Logger] = None
api_key: Optional[str] = None
server_url: Optional[str] = None

verify_cert: bool = True
max_retry: int = 3
max_backoff_time: int = 600
min_backoff_time: int = 5

timeout: int = 10

outgoing_proxy = None  # this is with respect to client. proxy for calls going out of customer environment. ( to armorcode).
inward_proxy = None

# throttling to 25 requests per seconds to avoid rate limit errors
rate_limiter = None

upload_to_ac = False


def main() -> None:
    global api_key, server_url, logger, exponential_time_backoff, verify_cert, timeout, rate_limiter, inward_proxy, outgoing_proxy, upload_to_ac

    parser = argparse.ArgumentParser()
    parser.add_argument("--serverUrl", required=False, help="Server Url")
    parser.add_argument("--apiKey", required=False, help="Api Key")
    parser.add_argument("--index", required=False, help="Agent index no", default="_prod")
    parser.add_argument("--timeout", required=False, help="timeout", default=30)
    parser.add_argument("--verify", required=False, help="Verify Cert", default=True)
    parser.add_argument("--debugMode", required=False, help="Enable debug Mode", default=True)

    parser.add_argument("--inwardProxyHttps", required=False, help="Pass inward Https proxy", default=None)
    parser.add_argument("--inwardProxyHttp", required=False, help="Pass inward Http proxy", default=None)

    parser.add_argument("--outgoingProxyHttps", required=False, help="Pass outgoing Https proxy", default=None)
    parser.add_argument("--outgoingProxyHttp", required=False, help="Pass outgoing Http proxy", default=None)
    parser.add_argument("--uploadToAc", action="store_true", help="Upload to Armorcode instead of s3 (default: False)",
                        default=False)

    args = parser.parse_args()

    server_url = args.serverUrl
    api_key = args.apiKey
    agent_index: str = args.index
    timeout_cmd = args.timeout
    verify_cmd = args.verify
    debug_cmd = args.debugMode
    upload_to_ac = args.uploadToAc

    inward_proxy_https = args.inwardProxyHttps
    inward_proxy_http = args.inwardProxyHttp

    outgoing_proxy_https = args.outgoingProxyHttps
    outgoing_proxy_http = args.outgoingProxyHttp

    if inward_proxy_https is None and inward_proxy_http is None:
        inward_proxy = None
    else:
        inward_proxy = {}
        if inward_proxy_https is not None:
            inward_proxy['https'] = inward_proxy_https
        if inward_proxy_http is not None:
            inward_proxy['http'] = inward_proxy_http

    if outgoing_proxy_https is None and outgoing_proxy_http is None:
        outgoing_proxy = None
    else:
        outgoing_proxy = {}
        if outgoing_proxy_https is not None:
            outgoing_proxy['https'] = outgoing_proxy_https
        if outgoing_proxy_http is not None:
            outgoing_proxy['http'] = outgoing_proxy_http

    debug_mode = True
    if debug_cmd is not None:
        if str(debug_cmd).lower() == "false":
            debug_mode = False

    if verify_cmd is not None:
        if str(verify_cmd).lower() == "false":
            verify_cert = False

    if timeout_cmd is not None:
        timeout = int(timeout_cmd)

    if os.getenv('verify') is not None:
        if str(os.getenv('verify')).lower() == "false":
            verify_cert = False

    if os.getenv("timeout") is not None:
        timeout = int(os.getenv("timeout"))

    logger = setup_logger(agent_index, debug_mode)

    # Fallback to environment variables if not provided as arguments
    if server_url is None:
        server_url = os.getenv('server_url')
    if api_key is None:
        api_key = os.getenv("api_key")

    logger.info("Agent Started for url %s, verify %s, timeout %s, outgoing proxy %s, inward %s, uploadToAc %s", server_url,
                verify_cert, timeout, outgoing_proxy, inward_proxy, upload_to_ac)

    if server_url is None or api_key is None:
        logger.error("Empty serverUrl %s", server_url)
        raise ValueError("Server URL and API Key must be provided either as arguments or environment variables")

    # Creating thread pool to use other thread if one thread is blocked in I/O
    # pool: concurrent.futures.ThreadPoolExecutor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    # pool.submit(process)
    # pool.submit(process)
    #
    # pool.shutdown(wait=True)

    # Instantiate RateLimiter for 25 requests per 15 seconds window
    rate_limiter = RateLimiter(request_limit=25, time_window=15)
    process()


def process() -> None:
    headers: Dict[str, str] = _get_headers()
    thread_backoff_time: int = min_backoff_time
    while True:
        try:
            # Get the next task for the agent
            logger.info("Requesting task...")
            rate_limiter.throttle()

            get_task_response: requests.Response = requests.get(
                f"{server_url}/api/http-teleport/get-task",
                headers=headers,
                timeout=25, verify=verify_cert,
                proxies=outgoing_proxy
            )

            if get_task_response.status_code == 200:
                thread_backoff_time = min_backoff_time
                task: Optional[Dict[str, Any]] = get_task_response.json().get('data', None)
                if task is None:
                    logger.info("Received empty task")
                    time.sleep(5)  # Wait before requesting next task
                    continue

                logger.info("Received task: %s", task['taskId'])
                task["version"] = __version__
                # Process the task

                result: Dict[str, Any] = process_task(task)

                # Update the task status
                update_task(result)
            elif get_task_response.status_code == 204:
                logger.info("No task available. Waiting...")
                time.sleep(5)
            elif get_task_response.status_code > 500:
                logger.error("Getting 5XX error %d, increasing backoff time", get_task_response.status_code)
                time.sleep(thread_backoff_time)
                thread_backoff_time = min(max_backoff_time, thread_backoff_time * 2)
            else:
                logger.error("Unexpected response: %d", get_task_response.status_code)
                time.sleep(5)

        except requests.exceptions.RequestException as e:
            logger.error("Network error: %s", e)
            time.sleep(10)  # Wait longer on network errors
        except Exception as e:
            logger.error("Unexpected error while processing: %s", e)
            time.sleep(5)


def update_task(task: Optional[Dict[str, Any]], count: int = 0) -> None:
    if task is None:
        return
    # Update the task status
    if count > max_retry:
        logger.error("Retry count exceeds for task %s", task['taskId'])
        return
    try:
        rate_limiter.throttle()
        update_task_response: requests.Response = requests.post(
            f"{server_url}/api/http-teleport/put-result",
            headers=_get_headers(),
            json=task,
            timeout=30, verify=verify_cert, proxies=outgoing_proxy
        )

        if update_task_response.status_code == 200:
            logger.info("Task %s updated successfully. Response: %s", task['taskId'],
                        update_task_response.text)
        elif update_task_response.status_code == 429 or update_task_response.status_code == 504:
            time.sleep(2)
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
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    return headers


def process_task(task: Dict[str, Any]) -> Dict[str, Any]:
    url: str = task.get('url')
    input_data: Any = task.get('input')
    taskId: str = task.get('taskId')
    headers: Dict[str, str] = task.get('requestHeaders', {})
    method: str = task.get('method').upper()
    expiryTime: int = task.get('expiryTsMs', round((time.time() + 300) * 1000))
    logger.info("Processing task %s: %s %s", taskId, method, url)

    # creating temp file to store outputs
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
        timeout = round((expiryTime - round(time.time() * 1000)) / 1000)
        logger.info("expiry %s, %s", expiryTime, timeout)

        logger.debug("Request for task %s with headers %s and input_data %s", taskId, headers, input_data)
        check_and_update_encode_url(headers, url)
        response: requests.Response = requests.request(method, url, headers=headers, data=input_data, stream=True,
                                                       timeout=timeout, verify=verify_cert, proxies=inward_proxy)
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
                data = response.content  # Entire response is downloaded
                with open(temp_output_file.name, 'wb') as f:
                    f.write(data)
        else:
            logger.debug("Status code is not 200 , response is %s", response.content)
            data = response.content  # Entire response is downloaded if request failed
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
        task['output'] = f"Network error: {str(e)}"
    except Exception as e:
        logger.error("Unexpected error processing task %s: %s", taskId, e)
        task['statusCode'] = 500
        task['output'] = f"Error: {str(e)}"
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
    if upload_to_ac:
        try:
            success = zip_response(temp_file, temp_file_zip)
            file_path = temp_file_zip if success else temp_file
            task['responseZipped'] = success
            file_name = f"{taskId}_{uuid.uuid4().hex}.{'zip' if success else 'txt'}"
            headers: Dict[str, str] = {
                "Authorization": f"Bearer {api_key}",
            }
            task_json = json.dumps(task)
            files = {
                # 'fileFieldName' is the name of the form field expected by the server
                "file": (file_name, open(file_path, "rb"), f"{'application/zip' if success else 'text/plain'}"),
                "task": (None, task_json, "application/json")
                # If you have multiple files, you can add them here as more entries
            }
            rate_limiter.throttle()
            upload_result: requests.Response = requests.post(
                f"{server_url}/api/http-teleport/upload-result",
                headers=headers,
                timeout=300, verify=verify_cert, proxies=outgoing_proxy, files=files
            )
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

    def allow_request(self) -> bool:

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
            time.sleep(0.5)


def upload_s3(temp_file,preSignedUrl: str, headers: Dict[str, Any]) -> bool:
    headersForS3: Dict[str, str] = {}
    if 'Content-Encoding' in headers and headers['Content-Encoding'] is not None:
        headersForS3['Content-Encoding'] = headers['Content-Encoding']
    if 'Content-Type' in headers and headers['Content-Type'] is not None:
        headersForS3['Content-Type'] = headers['Content-Type']

    try:
        with open(temp_file, 'rb') as file:
            response: requests.Response = requests.put(preSignedUrl, headers=headersForS3, data=file,
                                                       verify=verify_cert, proxies=outgoing_proxy, timeout=120)
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
    params: Dict[str, str] = {'fileName': f"{taskId}{uuid.uuid4().hex}"}
    try:
        rate_limiter.throttle()
        get_s3_url: requests.Response = requests.get(
            f"{server_url}/api/http-teleport/upload-url",
            params=params,
            headers=_get_headers(),
            timeout=25, verify=verify_cert, proxies=outgoing_proxy
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


if __name__ == "__main__":
    _createFolder(armorcode_folder)  # create parent level folder for logs anf files
    _createFolder(log_folder)  # create folder to store log files
    _createFolder(output_file_folder)  # create folder to store output files
    main()
