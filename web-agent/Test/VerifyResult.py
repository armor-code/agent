from enum import Enum

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

