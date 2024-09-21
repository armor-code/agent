#!/usr/bin/env python3
import argparse
import json
import os
import random
import string
import sys
import uuid
from typing import Optional

import requests
import time
import logging

# Configure logging to append to a file
logging.basicConfig(filename='output.txt', level=logging.INFO, filemode='a')
logging.basicConfig(filename='output.txt', level=logging.ERROR, filemode='a')

# Log messages will be appended to the file
logging.info("This log entry will be appended.")
logging.info("Another appended log entry.")

api_key = None
server_url = None

letters = string.ascii_letters
rand_string = ''.join(random.choice(letters) for _ in range(10))
output_file_folder = 'data'
output_file = f"{output_file_folder}/large_output_file.txt{rand_string}"

max_file_size = 1024 * 100 ##change this

def main():
    global api_key, server_url
    parser = argparse.ArgumentParser()
    parser.add_argument("--serverUrl", required=True, help="Database password")
    parser.add_argument("--apiKey", required=False, help="bucketName")
    args = parser.parse_args()

    server_url = args.serverUrl
    api_key = args.apiKey

    if api_key is None:
        api_key = os.getenv("api_key")

    headers = _get_headers()

    while True:
        try:
            # Get the next task
            logging.info("Requesting task...")
            get_task_response = requests.get(
                f"{server_url}/api/httpTeleport/getTask",
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
                    f"{server_url}/api/httpTeleport/putResult",
                    headers=_get_headers(),
                    json=result,
                    timeout=30
                )

                if update_task_response.status_code == 200:
                    logging.info("Task %s updated successfully. Response: %s", task['taskId'], update_task_response.text)
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
            logging.info("Error while processing task %s with error %s",)
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

    if method == 'POST':
        logging.info("Input is %s", input_data) ##todo: remove this log after testing
        input_data = json.loads(input_data)

    try:
        response = requests.request(method, url, headers=headers, json=input_data, stream=True, timeout=120)
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
                        with open(output_file, 'ab') as f:
                            f.write(chunk)

            else:
                logging.info("Non-chunked response, processing whole payload...")
                data = response.content  # Entire response is downloaded
                with open(output_file, 'ab') as f:
                    f.write(data)
        else:
            data = response.content  # Entire response is downloaded
            with open(output_file, 'ab') as f:
                f.write(data)

        s3_upload_url = None
        file_size = os.path.getsize(output_file)
        logging.info("file size %s", file_size)
        is_s3_upload = file_size > max_file_size
        if is_s3_upload:
            s3_upload_url = get_s3_upload_url(taskId)
            if s3_upload_url is None:
                logging.info("Failed to get S3 upload URL for URL ", url)
            upload_s3(s3_upload_url)

        # Collect response details
        _update_task_with_response(task, response, s3_upload_url, data)

        logging.info("Task %s processed successfully.", taskId)
        return task

    except Exception as e:
        logging.info("Error processing task %s: %s", taskId, e)
        task['output'] = str(e)
        return task

def _update_task_with_response(task, response, s3_upload_url, data):
    task['responseHeaders'] = dict(response.headers)
    task['statusCode'] = response.status_code  # statusCode
    if s3_upload_url is None:
        with open(output_file, 'r') as file:
            task['output'] = file.read()
    else:
        task['s3Url'] = s3_upload_url
    task.pop('latch')

def upload_s3(preSignedUrl):
    try:
        with open(output_file, 'rb') as file:
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

def get_s3_upload_url(taskId: str) -> Optional[str]:
    params = {'fileName': f"{taskId}{uuid.uuid1()}"}
    get_s3_url = requests.get(f"{server_url}/api/httpTeleport/uploadUrl", params=params,
                              headers=_get_headers(), timeout=25)

    if get_s3_url.status_code == 200:
        return get_s3_url.json().get('data', None)
    else:
        raise Exception("Unable to get signed URL", get_s3_url.status_code, get_s3_url.content)

if __name__ == "__main__":
    _createFolder()
    main()
