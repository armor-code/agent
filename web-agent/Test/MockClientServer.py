import os

from flask import Flask, request, jsonify, send_file
from werkzeug.serving import run_simple
import Resources.TestCases as TestCases

client_app = Flask("client_server")
test_cases = TestCases.test_cases
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPLOAD_DIR = os.path.join(BASE_DIR, 'Test', 'Resources', 'Files', 'S3_upload')


client_app = Flask("client_server")


@client_app.before_request
def initialize_tasks():
    return


@client_app.route('/<path:path>', methods=['GET'])
def serve_file(path):
    for case in test_cases:
        if case['url_path'] == f"/{path}":
            if path == "timeout":
                return "", 504
            elif path == "error_400":
                return "", 400
            elif path == "error_500":
                return "", 500
            elif case['response_file_path']:
                # Construct the full path to the file
                file_path = os.path.join(UPLOAD_DIR, os.path.basename(case['response_file_path']))
                if os.path.exists(file_path):
                    return send_file(
                        file_path,
                        mimetype=case['headers'].get('Content-Type', 'application/octet-stream'),
                        as_attachment=True
                    )
                else:
                    return f"File not found: {file_path}", 404
    return "", 404

if __name__ == "__main__":
    # Run both servers
    run_simple('localhost', 5001, client_app, use_reloader=True, use_debugger=True, use_evalex=True)