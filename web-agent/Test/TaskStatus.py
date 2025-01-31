from enum import Enum

class TaskStatusEnum(Enum):
    IN_QUEUE = "In Queue"
    IN_PROGRESS = "In Progress"
    FAILED = "Failed"
    PASSED = "Passed"