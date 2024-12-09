#!/usr/bin/env python3
import argparse
import os
import secrets
import string
import uuid
from collections import deque
from logging.handlers import TimedRotatingFileHandler
from typing import Optional, Tuple, Any, Dict

import requests
import logging
import time
from urllib.parse import unquote

# Global variables
letters: str = string.ascii_letters
rand_string: str = ''.join(secrets.choice(letters) for _ in range(10))
armorcode_folder: str = '/tmp/armorcode'
log_folder: str = '/tmp/armorcode/log'
output_file_folder: str = '/tmp/armorcode/output_files'
output_file: str = f"{output_file_folder}/large_output_file{rand_string}.txt"

max_file_size: int = 1024 * 100  # max_size data that would be sent in payload, more than that will send via s3
logger: Optional[logging.Logger] = None
api_key: Optional[str] = None
server_url: Optional[str] = None

# todo: different verify for
verify_cert: bool = True
max_retry: int = 3
max_backoff_time: int = 600
min_backoff_time: int = 5

timeout: int = 10

outgoing_proxy = None # this is with respect to client. proxy for calls going out of customer environment. ( to armorcode).
inward_proxy = None

# throttling to 25 requests per seconds to avoid rate limit errors
rate_limiter = None


def main() -> None:
    global api_key, server_url, logger, exponential_time_backoff, verify_cert, timeout, rate_limiter, inward_proxy, outgoing_proxy

    parser = argparse.ArgumentParser()
    parser.add_argument("--serverUrl", required=False, help="Server Url")
    parser.add_argument("--apiKey", required=False, help="Api Key")
    parser.add_argument("--index", required=True, help="Agent index no")
    parser.add_argument("--timeout", required=False, help="timeout", default=10)
    parser.add_argument("--verify", required=False, help="Verify Cert", default=True)
    parser.add_argument("--debugMode", required=False, help="Enable debug Mode", default=True)

    parser.add_argument("--inwardProxyHttps", required=False, help="Pass inward Https proxy", default=None)
    parser.add_argument("--inwardProxyHttp", required=False, help="Pass inward Http proxy", default=None)

    parser.add_argument("--outgoingProxyHttps", required=False, help="Pass outgoing Https proxy", default=None)
    parser.add_argument("--outgoingProxyHttp", required=False, help="Pass outgoing Http proxy", default=None)

    args = parser.parse_args()

    server_url = args.serverUrl
    api_key = args.apiKey
    agent_index: str = args.index
    timeout_cmd = args.timeout
    verify_cmd = args.verify
    debug_cmd = args.debugMode

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

    logger.info("Agent Started for url %s, verify %s, timeout %s, outgoing proxy %s, inward %s", server_url, verify_cert, timeout, outgoing_proxy, inward_proxy)

    if server_url is None or api_key is None:
        logger.error("Empty serverUrl or api Key %s", server_url)
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
        finally:
            # Remove the output generated file
            if os.path.exists(output_file):
                try:
                    os.remove(output_file)
                except OSError as e:
                    logger.error("Error removing output file: %s", e)


def update_task(task: Dict[str, Any], count: int = 0) -> None:
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

    try:
        # Running the request
        # timeout = round((expiryTime - round(time.time() * 1000)) / 1000)
        # logger.info("expiry %s, %s", expiryTime, timeout)

        logger.debug("Request for task %s with headers %s and input_data %s", taskId, headers, input_data)
        check_and_update_encode_url(headers, url)
        response: requests.Response = requests.request(method, url, headers=headers, data=input_data, stream=True,
                                                       timeout=timeout, verify=verify_cert, proxies=inward_proxy)
        logger.info("Response: %d", response.status_code)
        response.encoding = 'utf-8-sig'

        data: Any = None
        if response.status_code == 200:
            # Check if the response is chunked
            is_chunked: bool = response.headers.get('Transfer-Encoding', None) == 'chunked'

            if is_chunked:
                logger.info("Processing in chunks...")
                # Process the response in chunks
                for chunk in response.iter_content(chunk_size=1024 * 10):
                    if chunk:
                        with open(output_file, 'a') as f:
                            decoded_data = chunk.decode('utf-8-sig', errors='replace')
                            f.write(decoded_data)
            else:
                logger.info("Non-chunked response, processing whole payload...")
                data = response.text  # Entire response is downloaded
                with open(output_file, 'a') as f:
                    f.write(data)
        else:
            logger.debug("Status code is not 200 , response is %s", response.content)
            data = response.text  # Entire response is downloaded if request failed
            with open(output_file, 'a') as f:
                f.write(data)

        s3_signed_get_url: Optional[str] = None

        file_size: int = os.path.getsize(output_file)
        logger.info("file size %s", file_size)
        is_s3_upload: bool = file_size > max_file_size  # if size is greater than max_size, upload data to s3
        if is_s3_upload:
            s3_upload_url, s3_signed_get_url = get_s3_upload_url(taskId)
            if s3_upload_url is None:
                logger.warning("Failed to get S3 upload URL for URL %s", url)
            else:
                upload_success = upload_s3(s3_upload_url)

        # update task with the output
        _update_task_with_response(task, response, s3_signed_get_url)

        logger.info("Task %s processed successfully.", taskId)
        return task

    except requests.exceptions.RequestException as e:
        logger.error("Network error processing task %s: %s", taskId, e)
        task['output'] = f"Network error: {str(e)}"
    except Exception as e:
        logger.error("Unexpected error processing task %s: %s", taskId, e)
        task['output'] = f"Error: {str(e)}"

    return task


def check_and_update_encode_url(headers, url: str):
    if "/cxrestapi/auth/identity/connect/token" in url:
        headers["Content-Type"] = "application/x-www-form-urlencoded"


def _update_task_with_response(task: Dict[str, Any], response: requests.Response,
                               s3_signed_get_url: Optional[str]) -> None:
    task['responseHeaders'] = dict(response.headers)
    task['statusCode'] = response.status_code
    if s3_signed_get_url is None:  # check if needs to send data or fileURL
        with open(output_file, 'r') as file:
            task['output'] = file.read()
    else:
        task['s3Url'] = s3_signed_get_url


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


def upload_s3(preSignedUrl: str) -> bool:
    try:
        with open(output_file, 'r') as file:
            headers: Dict[str, str] = {
                "Content-Type": "application/json;charset=utf-8"
            }
            data: bytes = file.read().encode('utf-8', errors='replace')
            response: requests.Response = requests.put(preSignedUrl, headers=headers, data=data, verify=verify_cert, proxies=outgoing_proxy)
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
    params: Dict[str, str] = {'fileName': f"{taskId}{uuid.uuid4().hex}.txt"}
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
    log_filename: str = os.path.join("/tmp/armorcode/log", f"app_log{index}.log")

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

    return logger


if __name__ == "__main__":
    _createFolder(armorcode_folder)  # create parent level folder for logs anf files
    _createFolder(log_folder)  # create folder to store log files
    _createFolder(output_file_folder)  # create folder to store output files
    main()
