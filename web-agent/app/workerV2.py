#!/usr/bin/env python3
import argparse
import json
import logging
import os
import time
import uuid
import zipfile

# imports
from collections import deque
from logging.handlers import TimedRotatingFileHandler
from typing import Any, Dict, Callable, Optional, Tuple
import requests

# constants
logger: Optional[logging.Logger] = None
post_result_url_template = "{server_url}/api/http-teleport/put-result"
upload_file_url_template = "{server_url}/api/http-teleport/upload-result"
get_task_url_template = "{server_url}/api/http-teleport/get-task"
get_upload_url_template = "{server_url}/api/http-teleport/upload-url"

armorcode_folder: str = '/tmp/armorcode'
log_folder: str = '/tmp/armorcode/log'
output_file_folder: str = '/tmp/armorcode/output_files'
output_file_path_template = "/tmp/armorcode/output_files/{file_name}"

config = {}
global_vars = {
    'empty_task_sleep_time': 5,
    'get_task_timeout': 20,
    'get_upload_url_timeout': 20,
    'max_backoff_time': 30,
    'max_file_size': 1024 * 100,
    'max_retry_count_post_task_failure': 3,
    'max_retry_count_post_task_response': 3,
    'max_retry_count_upload_file': 3,
    'max_retry_count_zip': 3,
    'min_backoff_time': 5,
    'post_result_timeout': 20,
    'tool_timeout': 30,
    'upload_file_timeout': 60 * 5,
    'version': 'v2.0.1',
}


def main() -> None:
    global config, global_vars, logger
    parser = argparse.ArgumentParser()
    parser.add_argument("--serverUrl", required=False, help="Server Url")
    parser.add_argument("--apiKey", required=False, help="Api Key")
    parser.add_argument("--index", required=True, help="Agent index no")
    parser.add_argument("--timeout", required=False, help="timeout", default=10)

    parser.add_argument("--skipVerify", action="store_true", help="Verify Cert", default=False)
    parser.add_argument("--debugMode", action="store_true", help="Enable debug Mode", default=False)
    parser.add_argument("--uploadToAc", action="store_true", help="Upload to Armorcode instead of s3 (default: False)", default=False)

    parser.add_argument("--inwardProxyHttps", required=False, help="Pass inward Https proxy", default=None)
    parser.add_argument("--inwardProxyHttp", required=False, help="Pass inward Http proxy", default=None)

    parser.add_argument("--outgoingProxyHttps", required=False, help="Pass outgoing Https proxy", default=None)
    parser.add_argument("--outgoingProxyHttp", required=False, help="Pass outgoing Http proxy", default=None)

    args = parser.parse_args()
    config['verify_cert'] = not args.skipVerify
    config['debug_mode'] = args.debugMode
    config['upload_to_ac'] = args.uploadToAc

    if args.index is not None:
        config['index'] = int(args.index)
    else:
        config['index'] = 0
    
    logger = setup_logger(config['index'], config['debug_mode'])
    
    inward_proxy_https = args.inwardProxyHttps
    inward_proxy_http = args.inwardProxyHttp

    outgoing_proxy_https = args.outgoingProxyHttps
    outgoing_proxy_http = args.outgoingProxyHttp

    if inward_proxy_https is None and inward_proxy_http is None:
        config['inward_proxy'] = None
    else:
        config['inward_proxy'] = {}
        if inward_proxy_https is not None:
            config['inward_proxy']['https'] = inward_proxy_https
        if inward_proxy_http is not None:
            config['inward_proxy']['http'] = inward_proxy_http

    if outgoing_proxy_https is None and outgoing_proxy_http is None:
        config['outgoing_proxy'] = None
    else:
        config['outgoing_proxy'] = {}
        if outgoing_proxy_https is not None:
            config['outgoing_proxy']['https'] = outgoing_proxy_https
        if outgoing_proxy_http is not None:
            config['outgoing_proxy']['http'] = outgoing_proxy_http

    if args.timeout is not None:
        config['timeout'] = int(args.timeout)
    elif os.getenv("timeout") is not None:
        config['timeout'] = int(os.getenv("timeout"))
    else:
        config['timeout'] = 10

    config['server_url'] = args.serverUrl if args.serverUrl is not None else os.getenv("server_url")
    config['api_key'] = args.apiKey if args.apiKey is not None else os.getenv("api_key")

    logger.info(f"Agent Started for config: {config}, global_vars: {global_vars}")

    if config['server_url'] is None or config['api_key'] is None:
        logger.error("Empty serverUrl or api Key")
        raise ValueError("Server URL and API Key must be provided either as arguments or environment variables")
    
    global_vars['rate_limiter'] = RateLimiter(request_limit=25, time_window=15)
    inf_loop()
    

def inf_loop() -> None:
    thread_backoff_time: int = global_vars['min_backoff_time']
    while True:
        try:
            logger.info("Requesting task...")
            task = get_task()
            if task is None:
                time.sleep(global_vars['empty_task_sleep_time'])
                continue
            process_task(task)
            thread_backoff_time: int = global_vars['min_backoff_time']
        except Exception as e:
            logger.exception("Error in main loop: %s", e)
            time.sleep(thread_backoff_time)
            thread_backoff_time = min(global_vars['max_backoff_time'], thread_backoff_time * 2)


def get_task() -> Optional[Dict[str, Any]]:
    global_vars['rate_limiter'].throttle()
    url: str = get_task_url_template.format(server_url=config['server_url'])
    response: requests.Response = requests.get(
        url,
        headers=_get_json_headers(),
        timeout=global_vars['get_task_timeout'],
        verify=config['verify_cert'],
        proxies=config['outgoing_proxy'])

    match response.status_code:
        case 200:
            task: Optional[Dict[str, Any]] = response.json().get('data', None)
            if task is None:
                logger.info("Received empty task")
            return task
        case 204:
            logger.info("No task available. Waiting...")
            return None
        case _:
            logger.error("Response code: %d, message: %s", response.status_code, response.text)
            raise Exception("Got an error")


def process_task(task: Dict[str, Any]) -> None:
    task_id: str = task.get('taskId')
    global_vars['file_name_identifier'] = uuid.uuid4().hex
    logger.info("Received task: %s", task_id)
    success, out_task = make_internal_call(task)
    if not success:
        function_with_retry(lambda: post_task_failure(out_task), 'post_task_failure', global_vars['max_retry_count_post_task_failure'])
        return

    if not out_task.get('fileUploadPending', False):
        function_with_retry(lambda: post_task_response(out_task), 'post_task_response', global_vars['max_retry_count_post_task_response'])
        return

    output_file_path = out_task.get('output')
    out_task.pop('output', None)
    out_task.pop('fileUploadPending', None)
    if config['upload_to_ac']:
        function_with_retry(lambda: upload_file_ac(out_task, output_file_path), 'upload_file_ac', global_vars['max_retry_count_upload_file'])
    else:
        function_with_retry(lambda: upload_file_s3(out_task, output_file_path), 'upload_file_s3', global_vars['max_retry_count_upload_file'])


def post_task_failure(task: Dict[str, Any]) -> None:
    # todo: what should be the header and status code in the response.
    post_task_response(task)
    return


def upload_file_s3(task: Dict[str, Any], output_file_path: str) -> Optional[bool]:
    task_id: str = task.get('taskId')
    output_file_name = get_output_file_name(task_id, task.get('isZipped', False))
    try:
        s3_upload_url, s3_signed_get_url = get_s3_upload_url(output_file_name)
        if s3_upload_url is None:
            logger.warning("Failed to get S3 upload URL for file %s", output_file_name)
            task['output'] = "Error: Failed to get S3 upload URL"
            post_task_failure(task)
            return False
        response: requests.Response
        if task['isZipped']:
            with open(output_file_path, "rb") as file:
                response = requests.put(
                    s3_upload_url,
                    data=file,
                    headers={"Content-Type": "application/zip"},
                    verify=config['verify_cert'],
                    proxies=config['outgoing_proxy'])
        else:
            with open(output_file_path, 'r') as file:
                headers: Dict[str, str] = {
                    "Content-Type": "application/zip" if task['isZipped'] else "application/json;charset=utf-8"
                }
                data: bytes = file.read().encode('utf-8', errors='replace')
                response = requests.put(
                    s3_upload_url,
                    headers=headers,
                    data=data,
                    verify=config['verify_cert'],
                    proxies=config['outgoing_proxy'])
        response.raise_for_status()
        logger.info('File uploaded successfully to S3')
        task['s3Url'] = s3_signed_get_url
        return post_task_response(task)
    except requests.exceptions.RequestException as e:
        logger.error("Network error uploading to S3: %s", e)
        return None
    except Exception as e:
        logger.exception("Unexpected error uploading to S3: %s", e)
        return None


def check_and_update_encode_url(headers, url: str):
    if "/cxrestapi/auth/identity/connect/token" in url:
        headers["Content-Type"] = "application/x-www-form-urlencoded"


def get_output_file_name(task_id: str, is_zip: bool = False) -> str:
    return f"{task_id}_{global_vars['file_name_identifier']}.{'txt' if is_zip else 'zip'}"


def get_output_file_path(task_id: str, is_zip: bool = False) -> str:
    return output_file_path_template.format(file_name=get_output_file_name(task_id, is_zip))


def make_internal_call(task: Dict[str, Any]) -> [bool, Dict[str, Any]]:
    task_id: str = task.get('taskId')
    url: str = task.get('url')
    input_data: Any = task.get('input')
    headers: Dict[str, str] = task.get('requestHeaders', {})
    method: str = task.get('method').upper()
    logger.info("Processing task %s: %s %s", task_id, method, url)
    try:
        logger.debug("Request for task %s with headers %s and input_data %s", task_id, headers, input_data)
        check_and_update_encode_url(headers, url)
        response: requests.Response = requests.request(
            method,
            url,
            headers=headers,
            data=input_data,
            stream=True,
            timeout=global_vars['tool_timeout'],
            verify=config['verify_cert'],
            proxies=config['inward_proxy'])
        logger.info("Response: %d", response.status_code)
        # response.encoding = 'utf-8-sig'

        output_file_path = get_output_file_path(task_id)
        output_file_path_zip = get_output_file_path(task_id, True)
        _delete_files([output_file_path, output_file_path_zip])
        if response.status_code == 200:
            # Check if the response is chunked
            is_chunked: bool = response.headers.get('Transfer-Encoding', None) == 'chunked'

            if is_chunked:
                logger.info("Processing in chunks...")
                # Process the response in chunks
                for chunk in response.iter_content(chunk_size=1024 * 10):
                    if chunk:
                        with open(output_file_path, 'a') as f:
                            decoded_data = chunk.decode('utf-8-sig', errors='replace')
                            f.write(decoded_data)
            else:
                logger.info("Non-chunked response, processing whole payload...")
                data = response.text  # Entire response is downloaded
                with open(output_file_path, 'a') as f:
                    f.write(data)
        else:
            logger.debug("Status code is not 200 , response is %s", response.content)
            data = response.text  # Entire response is downloaded if request failed
            with open(output_file_path, 'a') as f:
                f.write(data)

        task['responseHeaders'] = dict(response.headers)
        task['statusCode'] = response.status_code

        file_size: int = os.path.getsize(output_file_path)
        logger.info("file size %s", file_size)
        is_s3_upload: bool = file_size > global_vars['max_file_size']  # if size is greater than max_size, upload data to s3
        if not is_s3_upload:
            with open(output_file_path, 'r') as file:
                task['output'] = file.read()
        else:
            success = function_with_retry(lambda: zip_file(output_file_path, output_file_path_zip), "zip_file", global_vars['max_retry_count_zip'])
            if not success:
                task['output'] = "Error: Failed to zip the file"
                return False, task
            task['isZipped'] = True
            task['output'] = output_file_path
            task['fileUploadPending'] = True

        return True, task
    except requests.exceptions.RequestException as e:
        logger.error("Network error processing task %s: %s", task_id, e)
        task['output'] = f"Network error: {str(e)}"
        return False, task
    except Exception as e:
        logger.error("Unexpected error processing task %s: %s", task_id, e)
        task['output'] = f"Error: {str(e)}"
        return False, task


def upload_file_ac(task: Dict[str, Any], zip_file_path: str) -> Optional[bool]:
    task_id: str = task.get('taskId')
    task_json: str = ''
    try:
        task_json = json.dumps(task)

        url = upload_file_url_template.format(server_url=config['server_url'])
        files = {
            # 'fileFieldName' is the name of the form field expected by the server
            "file": (f"{task_id}_{uuid.uuid4().hex}.zip", open(zip_file_path, "rb"), "application/zip"),
            "task": (None, task_json, "application/json")
            # If you have multiple files, you can add them here as more entries
        }
        global_vars['rate_limiter'].throttle()
        response: requests.Response = requests.post(
            url,
            headers=_get_headers(),
            timeout=global_vars['upload_file_timeout'],
            verify=config['verify_cert'],
            proxies=config['outgoing_proxy'],
            files=files)
        if response.status_code == 200:
            logger.info("File uploaded successfully, task: %s, zip_file_path", task_json, zip_file_path)
            return True
        elif response.status_code == 429 or response.status_code == 504:
            logger.warning("Rate limit hit while uploading file, retrying again for task %s", task_id)
            return None
        else:
            logger.error("Failed to upload file, task %s: %s", task_id, response.text)
            return False
    except Exception as e:
        logger.error("Error uploading file for task %s: %s", task_json, e)
        return None


def post_task_response(task: Dict[str, Any]) -> Optional[bool]:
    task_id: str = task.get('taskId')
    try:
        url = post_result_url_template.format(server_url=config['server_url'])
        global_vars['rate_limiter'].throttle()
        response: requests.Response = requests.post(
            url,
            headers=_get_json_headers(),
            json=task,
            timeout=global_vars['post_result_timeout'],
            verify=config['verify_cert'],
            proxies=config['outgoing_proxy'])
        if response.status_code == 200:
            logger.info("Task %s updated successfully. Response: %s", task_id, response.text)
            return True
        elif response.status_code == 429 or response.status_code == 504:
            logger.warning("Rate limit hit while updating the task output, retrying again for task %s", task_id)
            return None
        else:
            logger.error("Failed to update task %s: %s", task_id, response.text)
            return False
    except Exception as e:
        logger.error("Error posting result of task %s: %s", task_id, e)
        return None


def function_with_retry(func: Callable[[], Optional[bool]], name: str, count: int) -> bool:
    # True -> success, False -> failure, None -> retry
    for i in range(count):
        match func():
            case True:
                return True
            case False:
                logger.error("Failed to execute function: %s after %d attempts", name, i)
                return False
            case None:
                logger.warning("Retrying function: %s, count: %d", name,i + 1)
                time.sleep(5)
    logger.error("Failed to execute function: %s after %d attempts", name,count)
    return False


def get_s3_upload_url(output_file_name: str) -> Tuple[Optional[str], Optional[str]]:
    params: Dict[str, str] = {'fileName': output_file_name}
    try:
        global_vars['rate_limiter'].throttle()
        get_s3_url: requests.Response = requests.get(
            get_upload_url_template.format(server_url=config['server_url']),
            params=params,
            headers=_get_json_headers(),
            timeout=global_vars['get_upload_url_timeout'],
            verify=config['verify_cert'],
            proxies=config['outgoing_proxy'])
        get_s3_url.raise_for_status()

        data: Optional[Dict[str, str]] = get_s3_url.json().get('data', None)
        if data is not None:
            return data.get('putUrl'), data.get('getUrl')
        logger.warning("No data returned when requesting S3 upload URL")
    except requests.exceptions.RequestException as e:
        logger.error("Network error getting S3 upload URL: %s", e)
    except Exception as e:
        logger.exception("Unexpected error getting S3 upload URL: %s", e)
    return None, None


def _get_headers() -> Dict[str, str]:
    headers: Dict[str, str] = {
        "Authorization": f"Bearer {config['api_key']}",
        "Ac-Agent": f"ArmorCode/{global_vars['version']}",
    }
    return headers


def _get_json_headers() -> Dict[str, str]:
    headers: Dict[str, str] = _get_headers()
    headers['Content-Type'] = 'application/json'
    return headers


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


# OS utilities


def _delete_files(files: [str]) -> None:
    for file in files:
        try:
            if os.path.exists(file):
                os.remove(file)
        except OSError as e:
            logger.error("Error removing file: %s", e)


def _create_folder(folder_path: str) -> None:
    if not os.path.exists(folder_path):  # Check if the directory exists
        try:
            os.mkdir(folder_path)  # Create the directory if it doesn't exist
            print("Created output directory: %s", folder_path)
        except Exception as e:
            print("Error creating output folder: %s", folder_path)
    else:
        print("Output directory already exists: %s", folder_path)


def zip_file(input_file: str, output_file: str) -> Optional[bool]:
    if os.path.exists(input_file):
        try:
            with zipfile.ZipFile(output_file, 'w') as zipf:
                zipf.write(input_file)
            logger.debug("File zipped successfully: input: %s, output: %s", input_file, output_file)
            return True
        except Exception as e:
            logger.error("Error zipping file: %s", e)
            return None
    logger.error("Input file does not exist: %s", input_file)
    return False


def setup_logger(index: str, debug_mode: bool) -> logging.Logger:
    log_filename: str = os.path.join("/tmp/armorcode/log", f"app_log{config['index']}.log")

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
    _create_folder(armorcode_folder)  # create parent level folder for logs anf files
    _create_folder(log_folder)  # create folder to store log files
    _create_folder(output_file_folder)  # create folder to store output files
    main()
