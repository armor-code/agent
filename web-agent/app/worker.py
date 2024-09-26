#!/usr/bin/env python3
import argparse
import os
import random
import string
import uuid
from logging.handlers import TimedRotatingFileHandler
from typing import Optional, Tuple, Any, Dict

import requests
import logging
import time

# Global variables
letters: str = string.ascii_letters
rand_string: str = ''.join(random.choice(letters) for _ in range(10))
output_file_folder: str = './'
output_file: str = f"{output_file_folder}/large_output_file{rand_string}.txt"

max_file_size: int = 1024 * 10  # max_size data that would be sent in payload, more than that will send via s3
logger: Optional[logging.Logger] = None
api_key: Optional[str] = None
server_url: Optional[str] = None


def main() -> None:
    global api_key, server_url, logger

    parser = argparse.ArgumentParser()
    parser.add_argument("--serverUrl", required=False, help="Server Url")
    parser.add_argument("--apiKey", required=False, help="Api Key")
    parser.add_argument("--index", required=True, help="Agent index no")

    args = parser.parse_args()

    server_url = args.serverUrl
    api_key = args.apiKey
    agent_index: str = args.index

    logger = setup_logger(agent_index)

    _createFolder()  # create folder to store output files

    # Fallback to environment variables if not provided as arguments
    if server_url is None:
        server_url = os.getenv('server_url')
    if api_key is None:
        api_key = os.getenv("api_key")

    if server_url is None or api_key is None:
        logger.error("Empty serverUrl or api Key %s", server_url)
        raise ValueError("Server URL and API Key must be provided either as arguments or environment variables")

    headers: Dict[str, str] = _get_headers()

    # Main loop to continuously process tasks
    while True:
        try:
            # Get the next task for the agent
            logger.info("Requesting task...")
            get_task_response: requests.Response = requests.get(
                f"{server_url}/api/http-teleport/get-task",
                headers=headers,
                timeout=25)

            if get_task_response.status_code == 200:
                task: Optional[Dict[str, Any]] = get_task_response.json().get('data', None)
                if task is None:
                    logger.info("Received empty task")
                    time.sleep(5)  # Wait before requesting next task
                    continue

                logger.info("Received task: %s", task['taskId'])

                # Process the task
                result: Dict[str, Any] = process_task(task)

                # Update the task status
                update_task_response: requests.Response = requests.post(
                    f"{server_url}/api/http-teleport/put-result",
                    headers=_get_headers(),
                    json=result,
                    timeout=30
                )

                if update_task_response.status_code == 200:
                    logger.info("Task %s updated successfully. Response: %s", task['taskId'],
                                update_task_response.text)
                else:
                    logger.warning("Failed to update task %s: %s", task['taskId'], update_task_response.text)
                    # Consider implementing a retry mechanism here

            elif get_task_response.status_code == 204:
                logger.info("No task available. Waiting...")
                time.sleep(5)
            else:
                logger.warning("Unexpected response: %d", get_task_response.status_code)
                time.sleep(5)

        except requests.exceptions.RequestException as e:
            logger.error("Network error: %s", e)
            time.sleep(10)  # Wait longer on network errors
        except Exception as e:
            logger.error("Unexpected error while processing: %s", e, exc_info=True)
            time.sleep(5)
        finally:
            # Remove the output generated file
            if os.path.exists(output_file):
                try:
                    os.remove(output_file)
                except OSError as e:
                    logger.error("Error removing output file: %s", e)


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

    logger.info("Processing task %s: %s %s", taskId, method, url)

    try:
        # Running the request
        response: requests.Response = requests.request(method, url, headers=headers, data=input_data, stream=True,
                                                       timeout=120,
                                                       verify=False)
        logger.info("Response: %d", response.status_code)

        data: Optional[bytes] = None
        if response.status_code == 200:
            # Check if the response is chunked
            is_chunked: bool = response.headers.get('Transfer-Encoding', None) == 'chunked'

            if is_chunked:
                logger.info("Processing in chunks...")
                # Process the response in chunks
                for chunk in response.iter_content(chunk_size=1024 * 10):
                    if chunk:
                        with open(output_file, 'a') as f:
                            f.write(chunk.decode('utf-8'))
            else:
                logger.info("Non-chunked response, processing whole payload...")
                data = response.content  # Entire response is downloaded
                with open(output_file, 'a') as f:
                    f.write(data.decode('utf-8'))
        else:
            data = response.content  # Entire response is downloaded if request failed
            with open(output_file, 'a') as f:
                f.write(data.decode('utf-8'))

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
                if not upload_success:
                    logger.error("Failed to upload file to S3")
                    # Consider how to handle this failure (e.g., retry, mark task as failed)

        # update task with the output
        _update_task_with_response(task, response, s3_signed_get_url)

        logger.info("Task %s processed successfully.", taskId)
        return task

    except requests.exceptions.RequestException as e:
        logger.error("Network error processing task %s: %s", taskId, e)
        task['output'] = f"Network error: {str(e)}"
    except Exception as e:
        logger.error("Unexpected error processing task %s: %s", taskId, e, exc_info=True)
        task['output'] = f"Error: {str(e)}"

    return task


def _update_task_with_response(task: Dict[str, Any], response: requests.Response,
                               s3_signed_get_url: Optional[str]) -> None:
    task['responseHeaders'] = dict(response.headers)
    task['statusCode'] = response.status_code
    if s3_signed_get_url is None:  # check if needs to send data or fileURL
        with open(output_file, 'r') as file:
            task['output'] = file.read()
    else:
        task['s3Url'] = s3_signed_get_url


def upload_s3(preSignedUrl: str) -> bool:
    try:
        with open(output_file, 'r') as file:
            headers: Dict[str, str] = {
                "Content-Type": "application/json;charset=utf-8"
            }
            data: bytes = file.read().encode('utf-8')
            response: requests.Response = requests.put(preSignedUrl, headers=headers, data=data)
            response.raise_for_status()
            logger.info('File uploaded successfully to S3')
            return True
    except requests.exceptions.RequestException as e:
        logger.error("Network error uploading to S3: %s", e)
        raise
    except Exception as e:
        logger.exception("Unexpected error uploading to S3: %s", e)
        raise



def _createFolder() -> None:
    if not os.path.exists(output_file_folder):  # Check if the directory exists
        try:
            os.mkdir(output_file_folder)  # Create the directory if it doesn't exist
            logger.info("Created output directory: %s", output_file_folder)
        except Exception as e:
            logger.error("Error creating output folder: %s", e)
            raise  # Re-raise the exception as this is a critical error
    else:
        logger.info("Output directory already exists: %s", output_file_folder)


# Custom logger function for error logger with exc_info=True by default
def log_error(msg: str, *args: Any, **kwargs: Any) -> None:
    logger.error(msg, *args, exc_info=True, **kwargs)


def get_s3_upload_url(taskId: str) -> Tuple[Optional[str], Optional[str]]:
    params: Dict[str, str] = {'fileName': f"{taskId}{uuid.uuid4().hex}.txt"}
    try:
        get_s3_url: requests.Response = requests.get(
            f"{server_url}/api/http-teleport/upload-url",
            params=params,
            headers=_get_headers(),
            timeout=25
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
def setup_logger(index: str) -> logging.Logger:
    log_filename: str = os.path.join("", f"app1_log{index}.log")

    # Create a TimedRotatingFileHandler
    handler: TimedRotatingFileHandler = TimedRotatingFileHandler(
        log_filename, when="midnight", interval=1, backupCount=7
    )  # This will keep logs for the last 7 days

    # Set the log format
    formatter: logging.Formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    handler.setFormatter(formatter)

    # Create the logger instance
    logger: logging.Logger = logging.getLogger(__name__)
    logger.setLevel(logging.INFO)  # Set the log level (DEBUG, INFO, etc.)

    logger.addHandler(handler)

    return logger


if __name__ == "__main__":
    main()
