from enum import Enum

import json
import os

import json
import os

class TestStateManager:
    _file_path = 'Resources/Files/test_state.json'

    def get_task_type(self):
        if not os.path.exists(self._file_path):
            return "default_value"
        with open(self._file_path, 'r') as f:
            data = json.load(f)
        return data.get("task_type", "default_value")

    def set_task_type(self, task_type):
        data = {}
        # Load existing data if the file exists
        if os.path.exists(self._file_path):
            with open(self._file_path, 'r') as f:
                try:
                    data = json.load(f)
                except json.JSONDecodeError:
                    # Handle empty or malformed JSON file
                    data = {}
        # Update the task_type key
        data["task_type"] = task_type
        # Write back the updated data
        with open(self._file_path, 'w') as f:
            json.dump(data, f, indent=4)

    def reset_state(self):
        if os.path.exists(self._file_path):
            os.remove(self._file_path)
        # Create an empty JSON file
        with open(self._file_path, 'w') as f:
            json.dump({}, f, indent=4)

state_manager = TestStateManager()

class VerificationResult(Enum):
    SUCCESS = "success"
    FAILURE = "failure"

def verify_task_result(task, result):
    verification_functions = {
        'json_task': verify_json_task,
        'xml_task': verify_xml_task,
        'gzip_task': verify_gzip_task,
        'big_file_task': verify_big_file_task,
        'timeout_task': verify_error_task,
        'error_400_task': verify_error_task,
        'error_500_task': verify_error_task
    }

    verify_func = verification_functions.get(task['task_id'])
    if verify_func:
        return verify_func(task, result)
    else:
        return VerificationResult.FAILURE, "Unknown task type"

def verify_json_task(task, result):
    if 'Content-Type' in result.get('responseHeaders', {}) and result['responseHeaders']['Content-Type'] == 'application/json':
        return VerificationResult.SUCCESS, "JSON task verified successfully"
    return VerificationResult.FAILURE, "JSON task verification failed"

def verify_xml_task(task, result):
    if 'Content-Type' in result.get('responseHeaders', {}) and result['responseHeaders']['Content-Type'] == 'application/xml':
        return VerificationResult.SUCCESS, "XML task verified successfully"
    return VerificationResult.FAILURE, "XML task verification failed"

def verify_gzip_task(task, result):
    if 'Content-Encoding' in result.get('responseHeaders', {}) and result['responseHeaders']['Content-Encoding'] == 'gzip':
        return VerificationResult.SUCCESS, "GZIP task verified successfully"
    return VerificationResult.FAILURE, "GZIP task verification failed"

def verify_big_file_task(task, result):
    # Add specific verification for big file task if needed
    return VerificationResult.SUCCESS, "Big file task received"

def verify_error_task(task, result):
    expected_status = int(task['task_id'].split('_')[1])
    if 'status' in result and result['status'] == expected_status:
        return VerificationResult.SUCCESS, f"{task['task_id']} verified successfully"
    return VerificationResult.FAILURE, f"{task['task_id']} verification failed"

