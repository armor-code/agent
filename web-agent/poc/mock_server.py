#!/usr/bin/env python3
"""
Mock Server for Agent POC
- GET /get-task: Returns a random task with 5-15 iterations and 6-char random word
- POST /complete-task: Marks task as complete
"""

from flask import Flask, request, jsonify
import random
import string
import uuid
from threading import Lock

app = Flask(__name__)

# In-memory task storage
tasks = {}
completed_tasks = []
tasks_lock = Lock()
task_sequence = 0  # Global sequence counter


@app.route('/get-task', methods=['GET'])
def get_task():
    """Return a new task with random iterations (5-15) and sequential name"""
    global task_sequence

    task_id = str(uuid.uuid4())
    iterations = random.randint(5, 15)

    with tasks_lock:
        task_sequence += 1
        task_name = f"task_{task_sequence}_{iterations}"

    task = {
        'taskId': task_id,
        'taskName': task_name,
        'iterations': iterations,
        'timestamp': str(uuid.uuid1().time)
    }

    with tasks_lock:
        tasks[task_id] = task

    print(f"[SERVER] Created task: {task_id} - {task_name} ({iterations} iterations)")

    return jsonify({
        'status': 'success',
        'data': task
    }), 200


@app.route('/complete-task', methods=['POST'])
def complete_task():
    """Mark a task as complete"""
    data = request.get_json()
    task_id = data.get('taskId')

    if not task_id:
        return jsonify({
            'status': 'error',
            'message': 'taskId is required'
        }), 400

    with tasks_lock:
        if task_id not in tasks:
            return jsonify({
                'status': 'error',
                'message': f'Task {task_id} not found'
            }), 404

        completed_tasks.append(task_id)
        task = tasks[task_id]

    print(f"[SERVER] Completed task: {task_id} - {task.get('taskName')}")

    return jsonify({
        'status': 'success',
        'message': f'Task {task_id} completed'
    }), 200


@app.route('/stats', methods=['GET'])
def stats():
    """Return server statistics"""
    with tasks_lock:
        return jsonify({
            'total_tasks': len(tasks),
            'completed_tasks': len(completed_tasks),
            'pending_tasks': len(tasks) - len(completed_tasks)
        }), 200


if __name__ == '__main__':
    print("=" * 60)
    print("Mock Server for Agent POC")
    print("=" * 60)
    print("Endpoints:")
    print("  GET  /get-task       - Get a new random task")
    print("  POST /complete-task  - Mark task as complete")
    print("  GET  /stats          - Get server statistics")
    print("=" * 60)
    print("Starting server on http://localhost:5123")
    print("=" * 60)

    app.run(host='0.0.0.0', port=5123, debug=False, threaded=True)
