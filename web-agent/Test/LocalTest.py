import os

import requests


def test_mock_s3_upload():
    # URL of the mock S3 upload endpoint
    url = 'http://localhost:5000/mock-s3-upload'

    # Path to a test file
    test_file_path = 'test_file.txt'

    # Create a test file
    with open(test_file_path, 'w') as f:
        f.write('This is a test file for mock S3 upload.')

    # Open the file and send it in a PUT request
    with open(test_file_path, 'rb') as f:
        files = {'file': ('test_file.txt', f)}
        response = requests.put(url, files=files)

    # Print the response
    print(f"Status Code: {response.status_code}")
    print(f"Response: {response.json()}")

    # Clean up - remove the test file
    os.remove(test_file_path)

    # Check if the file was uploaded to the S3_upload folder
    uploaded_file_path = os.path.join('Resources', 'Files', 'S3_upload', 'test_file.txt')
    if os.path.exists(uploaded_file_path):
        print(f"File successfully uploaded to {uploaded_file_path}")
    else:
        print("File upload failed")

test_mock_s3_upload()