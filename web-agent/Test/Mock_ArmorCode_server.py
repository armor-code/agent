import json
import gzip
import os

import requests
from flask import Flask, request, jsonify, send_file
from werkzeug.serving import run_simple
import Resources.TestCases as TestCases

from werkzeug.utils import secure_filename
from  TaskStatus import TaskStatusEnum
from TestHelper import VerificationResult, verify_task_result, state_manager

import HttpTeleportTask

# Mock Armorcode Server
ac_app = Flask("ac_server")


task_map = {}
task_result_map = dict()

config_file = "config.properties"
# Function to read the variable from the file



# Armorcode Server Routes
@ac_app.before_request
def initialize_tasks():
    global task_map, task_result_map
    task_map = TestCases.retrun_task_map()
    task_keys = TestCases.return_key_list()
    for key in task_keys:
        task_map[key] = TaskStatusEnum.IN_QUEUE

@ac_app.route('/api/http-teleport/get-task', methods=['GET'])
def get_task():
    # Get taskType from query params, default to 'default' if not provided
    task_type = state_manager.get_task_type()
    task = task_map.get(task_type, None)

    if task is None:
        return jsonify({"error": "No task found"}), 404

    task_map[task_type] = TaskStatusEnum.IN_PROGRESS

    return jsonify({"data": {
        "taskId": task["task_id"],
        "url": f"http://localhost:5001{task['url_path']}",
        "method": "GET"
    }})

@ac_app.route('/api/http-teleport/put-result', methods=['POST'])
def put_result():
    print(f"Received result: {request.json}")

    result = request.json
    if not result or 'taskId' not in result:
        return jsonify({"status": "error", "message": "Invalid result format"}), 400

    task_id = result['taskId']
    task = next((t for t in TestCases.test_cases if t['task_id'] == task_id), None)

    if not task:
        return jsonify({"status": "error", "message": "Task not found"}), 404

    verification_result, message = verify_task_result(task, result)

    if verification_result == VerificationResult.SUCCESS:
        task_map[task_id] = TaskStatusEnum.PASSED
        return jsonify({"status": "success", "message": message}), 200

    else:
        task_map[task_id] = TaskStatusEnum.FAILED
        return jsonify({"status": "error", "message": message}), 400

@ac_app.route('/api/http-teleport/upload-result', methods=['POST'])
def upload_result():
    if 'file' not in request.files:
        return jsonify({"error": "No file part"}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No selected file"}), 400

    if 'task' not in request.form:
        return jsonify({"error": "No task data"}), 400

    task_data = request.form['task']

    if file:
        filename = secure_filename(file.filename)
        file.save(os.path.join(ac_app.config['UPLOAD_FOLDER'], filename))

        # Process the task data as needed
        # For example, you might want to save it to a database or perform some operation

        return jsonify({
            "data": f"File {filename} uploaded successfully for task",
            "success": True,
            "message": "Upload completed"
        }), 200

@ac_app.route('/api/http-teleport/upload-url', methods=['GET'])
def get_s3_path():
    task_id = request.args.get('fileName', '')
    return jsonify({
        "data": {
            "putUrl": f"http://localhost:5000/mock-s3-upload?taskId={task_id}",
            "getUrl": f"http://localhost:5000/mock-s3-download?taskId={task_id}"
        }
    })

@ac_app.route('/mock-s3-upload', methods=['PUT'])
def mock_s3_upload():
    task_id = request.args.get('taskId')
    if not task_id:
        return jsonify({"error": "Missing taskId parameter"}), 400
    if 'file' not in request.files:
        return jsonify({"error": "No file part"}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No selected file"}), 400

    if file:
        filename = secure_filename(file.filename)
        upload_folder = os.path.join('Resources', 'Files', 'S3_upload')

        if not os.path.exists(upload_folder):
            os.makedirs(upload_folder)

        file_path = os.path.join(upload_folder, filename)
        file.save(file_path)

        return jsonify({"message": "File uploaded successfully", "filename": filename}), 200



if __name__ == "__main__":
    run_simple('localhost', 5000, ac_app, use_reloader=True, use_debugger=True, use_evalex=True)