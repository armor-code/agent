#!/usr/bin/env python3
import argparse
import json
import os
import random
import string
import sys
import uuid
from typing import Optional, Tuple, Any

import requests
import time
import logging

api_key = None
server_url = None

letters = string.ascii_letters
rand_string = ''.join(random.choice(letters) for _ in range(10))
output_file_folder = 'data'
output_file = f"{output_file_folder}/large_output_file.txt{rand_string}"

max_file_size = 1024 * 100  ##change this


def main():
    global api_key, server_url
    parser = argparse.ArgumentParser()
    parser.add_argument("--serverUrl", required=False, help="Server Url")
    parser.add_argument("--apiKey", required=False, help="Api Key")
    parser.add_argument("--index", required=True, help="Agent index no")
    args = parser.parse_args()

    server_url = args.serverUrl
    api_key = args.apiKey
    agent_index = args.index

    # Configure logging to append to a file

    logging.basicConfig(
        filename='output_' + str(agent_index) + '.txt',
        level=logging.DEBUG,
        format='%(asctime)s - %(levelname)s - %(message)s',  # Format log messages with date, time, level, and message
        datefmt='%Y-%m-%d %H:%M:%S',
        filemode='a'
    )

    if server_url is None:
        server_url = os.getenv('server_url')

    if api_key is None:
        api_key = os.getenv("api_key")

    if server_url is None or api_key is None:
        logging.error("Empty serverUrl or api Key %s", server_url)

    headers = _get_headers()

    while True:
        try:
            # Get the next task
            logging.info("Requesting task...")
            get_task_response = requests.get(
                f"{server_url}/api/http-teleport/get-task",
                headers=headers,
                timeout=25)

            if get_task_response.status_code == 200:
                task = get_task_response.json().get('data', None)
                if task is None:
                    logging.info("Received empty task")
                    continue
                logging.info("Received task: %s", task['taskId'])

                # Process the task
                result = process_task(task)
                logging.info("Task processing result: %s", result)

                # Update the task
                update_task_response = requests.post(
                    f"{server_url}/api/http-teleport/put-result",
                    headers=_get_headers(),
                    json=result,
                    timeout=30
                )

                if update_task_response.status_code == 200:
                    logging.info("Task %s updated successfully. Response: %s", task['taskId'],
                                 update_task_response.text)
                else:
                    logging.info("Failed to update task %s: %s", task['taskId'], update_task_response.text)
            elif get_task_response.status_code == 204:
                logging.info("No task available. Waiting...")
                time.sleep(5)
            else:
                logging.info("Unexpected response: %d", get_task_response.status_code)
                time.sleep(5)
        except requests.exceptions.RequestException as e:
            logging.info("Error: %s", e)
            time.sleep(5)
        except Exception as e:
            logging.error("Error while processing %s", e)
        finally:
            # Remove the output generated file
            if os.path.exists(output_file):
                os.remove(output_file)


def _get_headers():
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    return headers


def process_task(task):
    tenant = task.get('tenant')
    url = task.get('url')
    input_data = task.get('input')
    taskId = task.get('taskId')
    headers = task.get('requestHeaders', {})
    method = task.get('method').upper()

    logging.info("Processing task %s: %s %s", taskId, method, url)

    try:
        if method == 'POST':
            logging.info("Input is %s", input_data)  ##todo: remove this log after testing
            # input_data = json.loads(input_data)

        response = requests.request(method, url, headers=headers, data=input_data, stream=True, timeout=120)
        logging.info("Response: %d", response.status_code)

        data = None
        if response.status_code == 200:
            # Check if the response is chunked
            is_chunked = response.headers.get('Transfer-Encoding', None) == 'chunked'

            if is_chunked:
                logging.info("Processing in chunks...")
                # Process the response in chunks
                for chunk in response.iter_content(chunk_size=1024 * 10):
                    if chunk:
                        with open(output_file, 'a') as f:
                            f.write(chunk.decode('utf-8'))

            else:
                logging.info("Non-chunked response, processing whole payload...")
                data = response.content  # Entire response is downloaded
                with open(output_file, 'a') as f:
                    f.write(data.decode('utf-8'))
        else:
            data = response.content  # Entire response is downloaded
            with open(output_file, 'a') as f:
                f.write(data.decode('utf-8'))

        s3_signed_get_url = None

        file_size = os.path.getsize(output_file)
        logging.info("file size %s", file_size)
        is_s3_upload = file_size > max_file_size
        if is_s3_upload:
            s3_upload_url, s3_signed_get_url = get_s3_upload_url(taskId)
            if s3_upload_url is None:
                logging.info("Failed to get S3 upload URL for URL ", url)
            upload_s3(s3_upload_url)

        # Collect response details
        _update_task_with_response(task, response, s3_signed_get_url, data)

        logging.info("Task %s processed successfully.", taskId)
        return task

    except Exception as e:
        logging.info("Error processing task %s: %s", taskId, e)
        task['output'] = str(e)
        return task


def _update_task_with_response(task, response, s3_signed_get_url, data):
    task['responseHeaders'] = dict(response.headers)
    task['statusCode'] = response.status_code  # statusCode
    if s3_signed_get_url is None:
        with open(output_file, 'r') as file:
            task['output'] = file.read()
    else:
        task['s3Url'] = s3_signed_get_url


def upload_s3(preSignedUrl):
    try:
        with open(output_file, 'r') as file:
            response = requests.put(preSignedUrl, data=file)
            response.raise_for_status()
            logging.info('File uploaded successfully')
            return True
    except Exception as e:
        logging.info("Error uploading to S3: %s", e)
        return False


def _createFolder():
    if not os.path.exists(output_file_folder):  # Check if the directory exists
        try:
            os.mkdir(output_file_folder)  # Create the directory if it doesn't exist
        except Exception as e:
            logging.info("An error occurred while creating the folder: %s", e)
    else:
        logging.info("Directory '%s' already exists.", output_file_folder)


# Custom logging function for error logging with exc_info=True by default
def log_error(msg, *args, **kwargs):
    logging.error(msg, *args, exc_info=True, **kwargs)


def get_s3_upload_url(taskId: str) -> tuple[Any, Any]:
    params = {'fileName': f"{taskId}{uuid.uuid1()}"}
    get_s3_url = requests.get(f"{server_url}/api/http-teleport/upload-url", params=params,
                              headers=_get_headers(), timeout=25)

    if get_s3_url.status_code == 200:
        data = get_s3_url.json().get('data', None)
        if data is not None:
            return data.get('putUrl'), data.get('getUrl')
        return get_s3_url.json().get('data', None)
    else:
        raise Exception("Unable to get signed URL", get_s3_url.status_code, get_s3_url.content)


if __name__ == "__main__":
    _createFolder()
    main()
