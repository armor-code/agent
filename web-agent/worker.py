#!/usr/bin/env python3
import argparse
import os
import random
import string
import sys
import uuid
from typing import Optional

import requests
import time

api_key = None
server_url = None

letters = string.ascii_letters
rand_string = ''.join(random.choice(letters) for _ in range(10))
output_file_folder = '/data'
output_file = output_file_folder + "/" + "large_output_file.txt" + rand_string

max_file_size = 1024 * 10


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
            print("Requesting task...")
            get_task_response = requests.get(
                f"{server_url}/api/httpTeleport/getTask",
                headers=headers,
                timeout=25)
            
            if get_task_response.status_code == 200:
                task = get_task_response.json().get('data', None)
                if task is None:
                    print(f"Received empty task", get_task_response)
                    return
                print(f"Received task: {task['taskId']}")

                # Process the task
                result = process_task(task)

                # Update the task
                update_task_response = requests.post(
                    f"{server_url}/api/httpTeleport/putResult",
                    headers=_get_headers(),
                    json=result,
                    timeout=30
                )

                if update_task_response.status_code == 200:
                    print(f"Task {task['taskId']} updated successfully.\n")
                else:
                    print(f"Failed to update task {task['taskId']}: {update_task_response.text}\n")
            elif get_task_response.status_code == 204:
                print("No task available. Waiting...")
                time.sleep(5)
            else:
                print(f"Unexpected response: {get_task_response.status_code}")
                time.sleep(5)
        except requests.exceptions.RequestException as e:
            print(f"Error: {e}")
            time.sleep(5)
        finally:
            ##remove the output generated file
            if os.path.exists(output_file):
                os.remove(output_file)
                return


def _get_headers():
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    return headers


def _validate_task(task):
    return


def process_task(task):
    tenant = task.get('tenant')
    url = task.get('url')
    input = task.get('input')
    taskId = task.get('taskId')
    headers = task.get('requestHeaders', {})
    method = task.get('method', 'GET').upper()

    print(f"Processing task {task['taskId']}: {method} {url}")

    try:
        response = requests.request(method, url, headers=headers, json=input, stream=True, timeout=120)
        print(f"Response: {response.status_code}")

        if response.status_code == 200:
            # Check if the response is chunked
            is_chunked = response.headers.get('Transfer-Encoding', None) == 'chunked'

            if is_chunked:
                print("Processing in chunks...")
                # Process the response in chunks
                for chunk in response.iter_content(chunk_size=1024 * 10):
                    if chunk:
                        with open(output_file, 'ab') as f:
                            f.write(chunk)

            else:
                print("Non-chunked response, processing whole payload...")
                data = response.content  # Entire response is downloaded
                with open(output_file, 'a') as f:
                    f.write(data)

        s3_upload_url = None
        file_size = os.path.getsize(output_file)
        is_s3_upload = file_size > max_file_size
        if is_s3_upload:
            s3_upload_url = get_s3_upload_url(taskId)
            if s3_upload_url is None:
                raise Exception("")
            upload_s3(s3_upload_url)

        # Collect response details
        _update_task_with_response(task, response, s3_upload_url)

        print(f"Task {task['taskId']} processed successfully.")
        return task

    except requests.exceptions.RequestException as e:
        print(f"Error processing task {task['taskId']}: {e}")
        task['output'] = str(e)
        return task


def _update_task_with_response(task, response, s3_upload_url):
    task['responseHeaders'] = dict(response.headers)
    task['statusCode'] = response.status_code
    if s3_upload_url is None:
        task['output'] = response.json()
    else:
        task['s3Url'] = s3_upload_url


def upload_s3(preSignedUrl):
    try:
        with open(output_file, 'rb') as file:
            response = requests.put(preSignedUrl, data=file)
            response.raise_for_status()
            print('File uploaded successfully')
            return True
    except Exception as e:
        print("Error uploading s3 signed  ")
        return False

def _createFolder():
    if not os.path.exists(output_file_folder):  # Check if the directory exists
        try:
            os.mkdir(output_file_folder)  # Create the directory if it doesn't exist
        except Exception as e:
            print(f"An error occurred while creating the folder: {e}")
    else:
        print(f"Directory '{output_file_folder}' already exists.")

def get_s3_upload_url(taskId: str) -> Optional[str]:

    params = {'fileName': taskId + str(uuid.uuid1())}
    get_s3_url = requests.get(f"{server_url}/api/httpTeleport/get-signed-url", params=params,
                              headers=_get_headers(), timeout=25)

    if get_s3_url.status_code == 200:
        return get_s3_url.json().get('data', None)
    else:
        raise Exception("Unable to get signedUrl ", get_s3_url.status_code, get_s3_url.content)



if __name__ == "__main__":
    _createFolder()
    main()
