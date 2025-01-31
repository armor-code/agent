
def retrun_task_map():
  return task_map

def return_key_list():
  return list(task_map.keys())

test_cases = [
  {
    "task_id": "json_task",
    "response_file_path": "json_response.json",
    "headers": {"Content-Type": "application/json"},
    "url_path": "/json",
    "is_zip": False
  },
  {
    "task_id": "xml_task",
    "response_file_path": "xml_response.xml",
    "headers": {"Content-Type": "application/xml"},
    "url_path": "/xml",
    "is_zip": False
  },
  {
    "task_id": "gzip_task",
    "response_file_path": "gzip_response.gz",
    "headers": {"Content-Type": "application/gzip", "Content-Encoding": "gzip"},
    "url_path": "/gzip",
    "is_zip": True
  },
  {
    "task_id": "big_file_task",
    "response_file_path": "big_file.zip",
    "headers": {"Content-Type": "application/zip"},
    "url_path": "/big_file",
    "is_zip": True
  },
  {
    "task_id": "timeout_task",
    "response_file_path": None,
    "headers": {},
    "url_path": "/timeout",
    "is_zip": False
  },
  {
    "task_id": "error_400_task",
    "response_file_path": None,
    "headers": {},
    "url_path": "/error_400",
    "is_zip": False
  },
  {
    "task_id": "error_500_task",
    "response_file_path": None,
    "headers": {},
    "url_path": "/error_500",
    "is_zip": False
  }
]
task_map = {task['task_id']: task for task in test_cases}