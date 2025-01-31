import os
import time
import unittest
import subprocess
import threading
import zipfile
from random import random
from urllib.request import urlretrieve

import Mock_ArmorCode_server
from MockClientServer import client_app
from Resources import TestCases

file_directory = "Resources/Files/FilesToUpload"

class TestArmorCodeAgent(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Create necessary directories
        os.makedirs(file_directory, exist_ok=True)

        # Create files mentioned in TestCases.py
        # for case in TestCases.test_cases:
        #     if case['response_file_path']:
        #         file_path = os.path.join(file_directory, case['response_file_path'])
        #         if case['task_id'] == 'big_file_task':
        #             # Create a 300 MB zip file
        #             cls.create_large_zip_file(file_path, 300 * 1024 * 1024)  # 300 MB in bytes
        #         else:
        #             with open(file_path, 'w') as f:
        #                 f.write(f"Mock content for {case['task_id']}")

        # Download worker.py and requirements.txt
        urlretrieve('https://raw.githubusercontent.com/armor-code/agent/refs/heads/main/web-agent/app/worker.py', 'worker.py')
        urlretrieve('https://raw.githubusercontent.com/armor-code/agent/refs/heads/main/web-agent/requirements.txt', 'requirements.txt')

        # Install requirements
        subprocess.run(['pip3', 'install', '-r', 'requirements.txt'])

        # Start Mock_ArmorCode_server
        # cls.ac_thread = threading.Thread(target=lambda: ac_app.run(port=5000))
        # cls.ac_thread.daemon = True
        # cls.ac_thread.start()
        #
        # # Start MockClientServer
        # cls.client_thread = threading.Thread(target=lambda: client_app.run(port=5001))
        # cls.client_thread.daemon = True
        # cls.client_thread.start()
        #
        # # Wait for servers to start
        # time.sleep(2)

    @staticmethod
    def create_large_zip_file(file_path, size_bytes):
        with zipfile.ZipFile(file_path, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            # Create a large file with random data
            chunk_size = 1024 * 1024  # 1 MB
            remaining_size = size_bytes
            while remaining_size > 0:
                chunk = os.urandom(min(chunk_size, remaining_size))

                zip_file.writestr(f"data_{remaining_size}.bin", chunk)
                remaining_size -= len(chunk)

    def setUp(self):
        # Clear task_result_map before each test
        Mock_ArmorCode_server.task_result_map.clear()

    def run_worker_and_wait(self, task_id):
        # Start worker process
        worker_process = subprocess.Popen(['python3', 'worker.py', '--serverUrl', 'http://localhost:5000', '--apiKey', 'test_api_key', '--index', 'test_index', '--timeout', '30', '--verify', 'False', '--debugMode', 'False']);

        # Wait for task result (max 60 seconds)
        start_time = time.time()
        while time.time() - start_time < 60:
            if task_id in Mock_ArmorCode_server.task_result_map:
                break
            time.sleep(1)

        # Stop worker process
        worker_process.terminate()
        worker_process.wait()

        return Mock_ArmorCode_server.task_result_map.get(task_id)

    def test_tasks(self):
        for case in TestCases.test_cases:
            with self.subTest(task_id=case['task_id']):
                # Set task type to return
                Mock_ArmorCode_server.write_global_var(case['task_id'])

                # Run worker and wait for result
                result = self.run_worker_and_wait(case['task_id'])

                # Assert that we got a result
                self.assertIsNotNone(result, f"No result received for task {case['task_id']}")

                # Add more specific assertions based on expected results
                # For example:
                if case['task_id'] == 'json_task':
                    self.assertEqual(result['status'], 'success')
                    self.assertIn('data', result)
                elif case['task_id'] == 'timeout_task':
                    self.assertEqual(result['status'], 'error')
                    self.assertIn('timeout', result['message'].lower())
                # Add more assertions for other task types

    @classmethod
    def tearDownClass(cls):
        # Clean up created files
        for case in TestCases.test_cases:
            if case['response_file_path']:
                file_path = os.path.join('Test/Resources/Files/S3_upload', case['response_file_path'])
                if os.path.exists(file_path):
                    os.remove(file_path)

        # Remove downloaded files
        if os.path.exists('worker.py'):
            os.remove('worker.py')
        if os.path.exists('requirements.txt'):
            os.remove('requirements.txt')

if __name__ == '__main__':
    unittest.main()