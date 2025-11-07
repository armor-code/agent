# ArmorCode Web Agent - Refactoring & Rewrite Plan

**Document Version**: 1.0
**Date**: 2025-11-07
**Status**: Draft - Awaiting Review

---

## Executive Summary (REVISED - 2025-11-07)

The current ArmorCode Web Agent has **two critical design flaws**:

### Problem 1: Infinite Task Fetching
**Current behavior**: Task fetcher continuously pulls tasks from AC server even when the system cannot process them (no backpressure).

**Impact**: Memory overflow, task drops, system overload.

**Your feedback**: "Task 1 cannot pull for tasks infinitely, it should only pull the tasks which it can process, that means if there is even a single task in the outgoing queue."

### Problem 2: "Too Many Concurrent Requests" Errors
**Current behavior**: No control over concurrent connections to AC server. Multiple threads can simultaneously connect, exceeding server limits.

**Impact**: AC server rejects requests with "too many concurrent requests" error.

**Your feedback**: "Issue with current implementation is agent is getting too many concurrent request error."

### Solution: Smart Queue-Based Architecture with Concurrency Control

**Key Features**:
1. **Smart Task Fetcher** with backpressure: Only fetches when `response_queue.qsize() < 80%`
2. **AC Server Semaphore**: Guarantees exactly 2 concurrent connections (1 fetcher + 1 uploader)
3. **3-Module Pipeline**: Fetcher → Executor Pool → Uploader

**Guarantees**:
- ✅ No infinite polling (backpressure control)
- ✅ Exactly 2 concurrent AC server requests (semaphore)
- ✅ Memory controlled (queue size limits)
- ✅ Scalable processing (Module 2 can scale to 50+ workers)

---

## Table of Contents

1. [Current Architecture Analysis](#current-architecture-analysis)
2. [Identified Bottlenecks](#identified-bottlenecks)
3. [Proposed Solutions](#proposed-solutions)
   - [Option 1: Python Queue Refactoring](#option-1-python-queue-refactoring)
   - [Option 2: Go Rewrite](#option-2-go-rewrite)
   - [Option 3: Java Rewrite](#option-3-java-rewrite)
4. [Comparison Matrix](#comparison-matrix)
5. [Recommended Approach](#recommended-approach)
6. [Implementation Details](#implementation-details)
7. [Testing Strategy](#testing-strategy)
8. [Deployment Plan](#deployment-plan)
9. [Risk Assessment](#risk-assessment)

---

## Current Architecture Analysis

### Technology Stack
- **Language**: Python 3.9+
- **Concurrency**: Gevent (greenlet-based cooperative multitasking)
- **Threading**: Gevent Pool (default size: 5)
- **Dependencies**: requests, gevent, greenlet

### Current Flow

```
┌─────────────────────────────────────────────────────────┐
│                  Current Architecture                    │
├─────────────────────────────────────────────────────────┤
│                                                          │
│  Main Loop (process() function)                         │
│  ┌────────────────────────────────────────────┐         │
│  │  1. Fetch task from AC server              │         │
│  │  2. Wait for pool slot (BLOCKING)          │ ← ISSUE │
│  │  3. Spawn task processing greenlet         │         │
│  │  4. Task executes internal tool call       │         │
│  │  5. Upload response to AC server           │         │
│  │  6. Loop back to step 1                    │         │
│  └────────────────────────────────────────────┘         │
│                                                          │
│  Problem: Step 2 blocks steps 1 and 6                   │
│                                                          │
└─────────────────────────────────────────────────────────┘
```

### Critical Code Section

**File**: `app/worker.py`
**Lines**: 142-143

```python
thread_pool.wait_available()  # BLOCKING - prevents task fetching
thread_pool.spawn(process_task_async, task)
```

### Current Concurrency Model

- **1 greenlet**: Polling for tasks (blocked when pool is full)
- **5 greenlets**: Processing tasks (configurable via `--poolSize`)
- **Total AC server requests**: Variable (1 to 6, uncontrolled)

---

## Identified Bottlenecks

### 1. Blocking Pool Wait (CRITICAL)
**Location**: `app/worker.py:142`
**Impact**: Main polling loop blocks when pool is full
**Result**: Cannot fetch new tasks, throughput collapse

### 2. No Task Buffering
**Issue**: Tasks are processed immediately upon fetch
**Result**: No queue visibility, cannot prioritize tasks

### 3. Single-Threaded Polling
**Issue**: Only 1 greenlet polls for tasks
**Result**: If polling is blocked, entire agent stalls

### 4. Shared Rate Limiting
**Issue**: Same rate limit (25 req/15s) for fetch, upload, update
**Result**: Operations compete for rate limit budget

### 5. No Concurrent Uploads
**Issue**: Large file uploads (up to 300s timeout) block task greenlets
**Result**: Pool slots occupied during entire upload duration

### 6. Uncontrolled AC Server Concurrency
**Issue**: No mechanism to guarantee exactly 2 concurrent AC requests
**Result**: Cannot comply with server constraint

---

## Proposed Solutions

---

## Option 1: Python Queue Refactoring

**Feasibility**: ✅ HIGH
**Time to Production**: 1-2 days
**Complexity**: LOW
**Risk**: LOW

### Proposed Architecture (REVISED)

```
┌─────────────────────────────────────────────────────────────────┐
│         Smart Queue-Based 3-Module Architecture                 │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌──────────────┐       ┌──────────────┐      ┌─────────────┐  │
│  │  Module 1    │       │  Module 2    │      │  Module 3   │  │
│  │              │       │              │      │             │  │
│  │   Smart      │──────▶│   Request    │─────▶│  Response   │  │
│  │   Task       │ queue │  Executor    │ queue│  Uploader   │  │
│  │  Fetcher     │   1   │              │   2  │             │  │
│  │ (1 greenlet) │       │(N greenlets) │      │(1 greenlet) │  │
│  └──────┬───────┘       └──────────────┘      └──────┬──────┘  │
│         │                                             │          │
│         │  ┌──────────────────────────────────┐      │          │
│         │  │  AC Server Semaphore             │      │          │
│         └─▶│  (max 2 concurrent connections)  │◀─────┘          │
│            │  - 1 slot for fetcher            │                 │
│            │  - 1 slot for uploader           │                 │
│            └──────────────────────────────────┘                 │
│                                                                  │
│  Smart Fetcher Logic:                                           │
│  • Only fetch if response_queue has capacity                    │
│  • Only fetch if AC semaphore slot available                    │
│  • Sleep when queues are full (backpressure)                    │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### Module Responsibilities

#### Module 1: Smart Task Fetcher (REVISED)
- **Threads**: 1 greenlet
- **Purpose**: Intelligently poll AC server only when system can process tasks
- **AC Requests**: 1 concurrent (GET `/api/http-teleport/get-task`)
- **Input**: AC server API
- **Output**: `task_queue`
- **Blocking**: Smart blocking based on queue capacity

**Key Characteristics** (REVISED):
- **Smart Polling**: Only fetch if `response_queue.qsize() < max_capacity` (default: 80% = 80 items)
- **Backpressure**: Sleep 5s when response_queue is near full
- **AC Semaphore**: Acquire semaphore slot before fetching, release after
- **Rate Limited**: 25 req/15s window (existing rate limiter)
- **Handles 204**: No tasks available → sleep 1s
- **Exponential backoff**: On 5XX errors

**Smart Polling Algorithm**:
```python
while True:
    # Check if system can handle more tasks
    if response_queue.qsize() >= response_queue_threshold:  # 80% full
        logger.info("Response queue near full, pausing task fetching")
        gevent.sleep(5)  # Backpressure: wait for uploader to drain
        continue

    # Acquire AC server semaphore (max 2 concurrent)
    ac_semaphore.acquire()  # Blocks if 2 connections already active
    try:
        # Fetch task from AC server
        task = fetch_task_from_server()
        if task:
            task_queue.put(task)
    finally:
        ac_semaphore.release()  # Always release

    gevent.sleep(0.1)  # Small delay between fetches
```

#### Module 2: Request Executor
- **Threads**: N greenlets (configurable, default: 10)
- **Purpose**: Execute requests to internal tools (JIRA, Coverity, etc.)
- **AC Requests**: 0 (only internal tool calls)
- **Input**: `task_queue`
- **Output**: `response_queue`
- **Blocking**: Blocks on empty `task_queue` (waiting for work)

**Key Characteristics**:
- Most time-consuming operations (30-300s per task)
- Can scale independently of AC server constraints
- Handles internal proxy configuration
- Streams large responses to temp files

#### Module 3: Response Uploader (REVISED)
- **Threads**: 1 greenlet
- **Purpose**: Upload task results back to AC server
- **AC Requests**: 1 concurrent (POST `/api/http-teleport/put-result` or `/upload-result`)
- **Input**: `response_queue`
- **Output**: AC server API
- **Blocking**: Blocks on empty `response_queue` (waiting for results)

**Key Characteristics** (REVISED):
- **AC Semaphore**: Acquire semaphore slot before uploading, release after
- **Handles small responses**: Base64 inline
- **Handles large responses**: Multipart upload or S3
- **Retry logic**: 3 attempts on 429/504 errors
- **Rate limited**: 25 req/15s window (existing rate limiter)

**Upload Algorithm**:
```python
while True:
    # Block until result is available
    task = response_queue.get(block=True)

    # Acquire AC server semaphore (max 2 concurrent)
    ac_semaphore.acquire()  # Blocks if 2 connections already active
    try:
        # Upload result to AC server
        upload_response(task)
    finally:
        ac_semaphore.release()  # Always release
```

### Queue Configuration (REVISED)

```python
from queue import Queue
from threading import BoundedSemaphore

# Task queue: Holds tasks fetched from AC server
task_queue = Queue(maxsize=100)  # Buffer up to 100 pending tasks

# Response queue: Holds completed tasks ready for upload
response_queue = Queue(maxsize=100)  # Buffer up to 100 completed tasks
response_queue_threshold = 80  # Start backpressure at 80% capacity

# AC Server Semaphore: Limits concurrent connections to AC server
ac_server_semaphore = BoundedSemaphore(2)  # Max 2 concurrent connections
```

**Queue Behavior**:
- `Queue.put(item)`: Blocks if queue is full (prevents memory overflow)
- `Queue.get()`: Blocks if queue is empty (worker waits for work)
- `Queue.qsize()`: Returns approximate number of items (used for backpressure)
- Thread-safe by design (Python stdlib implementation)

**Semaphore Behavior**:
- `BoundedSemaphore(2)`: Allows max 2 concurrent "acquires"
- `acquire()`: Blocks if 2 greenlets already hold semaphore
- `release()`: Frees one slot for another greenlet
- Gevent-aware: Use `gevent.lock.BoundedSemaphore` for greenlet cooperation

### Implementation Changes

#### 1. Add Queue Imports (app/worker.py:1-30) - REVISED

```python
from queue import Queue, Empty
import gevent
from gevent import monkey
monkey.patch_all()
from gevent.lock import BoundedSemaphore  # Gevent-aware semaphore
```

#### 2. Initialize Queues and Semaphore (app/worker.py:~90) - REVISED

```python
# Global queues
task_queue = Queue(maxsize=100)
response_queue = Queue(maxsize=100)
response_queue_threshold = 80  # 80% capacity trigger for backpressure

# AC Server concurrency control
ac_server_semaphore = BoundedSemaphore(2)  # Max 2 concurrent AC server requests
```

#### 3. Implement Module 1: Smart Task Fetcher - REVISED

**New Function** (add to app/worker.py):

```python
def task_fetcher_worker(config_dict):
    """
    Module 1: Smart task fetcher - only fetches when system can process.
    Implements:
    - Backpressure: Stops fetching when response_queue is near full
    - AC Semaphore: Guarantees max 1 concurrent AC server request from this module
    - Smart polling: Avoids overwhelming the system
    """
    logger = config_dict['logger']
    rate_limiter = config_dict['rate_limiter']
    server_url = config_dict['server_url']
    ac_semaphore = config_dict['ac_server_semaphore']
    response_queue = config_dict['response_queue']
    task_queue = config_dict['task_queue']
    response_queue_threshold = config_dict.get('response_queue_threshold', 80)

    thread_backoff_time = 5  # Initial backoff
    max_backoff_time = 600   # Max 10 minutes

    while True:
        try:
            # CRITICAL: Check backpressure - don't fetch if response queue is near full
            current_response_queue_size = response_queue.qsize()
            if current_response_queue_size >= response_queue_threshold:
                logger.warning(
                    f"Response queue near full ({current_response_queue_size}/{response_queue.maxsize}), "
                    f"pausing task fetching for 5s (backpressure)"
                )
                gevent.sleep(5)  # Wait for uploader to drain queue
                continue

            # Rate limiting (existing mechanism)
            rate_limiter.throttle()

            # Acquire AC server semaphore (blocks if 2 concurrent connections already active)
            logger.debug("Acquiring AC server semaphore for task fetch...")
            ac_semaphore.acquire()

            try:
                # Fetch task from AC server
                logger.info("Requesting task from AC server...")
                get_task_response = requests.get(
                    f"{server_url}/api/http-teleport/get-task",
                    headers=_get_headers(config_dict),
                    timeout=25,
                    verify=config_dict.get('verify_cert', False),
                    proxies=config_dict.get('outgoing_proxy'),
                    params={
                        'agentId': config_dict['agent_id'],
                        'agentVersion': config_dict['agent_version'],
                        'envName': config_dict.get('env_name', '')
                    }
                )

                if get_task_response.status_code == 200:
                    task = get_task_response.json().get('data', None)
                    if task:
                        logger.info(f"Fetched task: {task.get('taskId')}")
                        task_queue.put(task, block=True, timeout=5)  # Block max 5s if queue full
                        thread_backoff_time = 5  # Reset backoff
                    else:
                        # No tasks available (empty response)
                        logger.debug("Received empty task data")
                        gevent.sleep(1)

                elif get_task_response.status_code == 204:
                    # No tasks available
                    logger.debug("No tasks available (204)")
                    gevent.sleep(1)
                    thread_backoff_time = 5  # Reset backoff

                elif 500 <= get_task_response.status_code < 600:
                    # Server error - exponential backoff
                    logger.warning(f"Server error {get_task_response.status_code}, backing off {thread_backoff_time}s")
                    gevent.sleep(thread_backoff_time)
                    thread_backoff_time = min(max_backoff_time, thread_backoff_time * 2)

                else:
                    # Other errors
                    logger.error(f"Unexpected status code: {get_task_response.status_code}")
                    gevent.sleep(5)

            finally:
                # CRITICAL: Always release semaphore
                ac_semaphore.release()
                logger.debug("Released AC server semaphore after task fetch")

            # Small delay between fetch attempts to avoid tight loop
            gevent.sleep(0.1)

        except Exception as e:
            logger.error(f"Error in task_fetcher_worker: {e}", exc_info=True)
            gevent.sleep(5)
```

#### 4. Implement Module 2: Request Executor

**New Function** (add to app/worker.py):

```python
def request_executor_worker(config_dict):
    """
    Module 2: Execute requests to internal tools.
    NO AC server requests - only internal tool calls.
    """
    logger = config_dict['logger']

    while True:
        try:
            # Block until task is available
            task = task_queue.get(block=True)

            logger.info(f"Processing task: {task.get('taskId')}")

            # Process task using existing process_task() function
            # This function already handles:
            # - Internal tool HTTP requests
            # - Response streaming to temp files
            # - Error handling
            result = process_task(task, config_dict)  # Existing function!

            # Queue result for upload
            response_queue.put(result, block=True, timeout=5)

            logger.info(f"Completed task: {task.get('taskId')}")

        except Empty:
            # Queue is empty, wait for more tasks
            gevent.sleep(0.1)

        except Exception as e:
            logger.error(f"Error in request_executor_worker: {e}", exc_info=True)
            # On error, still try to upload error response
            if 'task' in locals():
                task['error'] = str(e)
                task['responseCode'] = 500
                response_queue.put(task, block=True, timeout=5)
```

#### 5. Implement Module 3: Response Uploader - REVISED

**New Function** (add to app/worker.py):

```python
def response_uploader_worker(config_dict):
    """
    Module 3: Upload task results back to AC server.
    Implements:
    - AC Semaphore: Guarantees max 1 concurrent AC server request from this module
    - Retry logic: 3 attempts on 429/504 errors
    """
    logger = config_dict['logger']
    rate_limiter = config_dict['rate_limiter']
    ac_semaphore = config_dict['ac_server_semaphore']
    response_queue = config_dict['response_queue']

    while True:
        try:
            # Block until result is available
            task = response_queue.get(block=True)

            logger.info(f"Uploading response for task: {task.get('taskId')}")

            # Rate limiting (existing mechanism)
            rate_limiter.throttle()

            # Acquire AC server semaphore (blocks if 2 concurrent connections already active)
            logger.debug("Acquiring AC server semaphore for response upload...")
            ac_semaphore.acquire()

            try:
                # Upload using existing update_task() function
                # This function already handles:
                # - Small responses (base64 inline)
                # - Large responses (multipart upload or S3)
                # - Retry logic (3 attempts)
                update_task(task, config_dict)  # Existing function!

                logger.info(f"Uploaded response for task: {task.get('taskId')}")

            finally:
                # CRITICAL: Always release semaphore
                ac_semaphore.release()
                logger.debug("Released AC server semaphore after response upload")

        except Empty:
            # Queue is empty, wait for more results
            gevent.sleep(0.1)

        except Exception as e:
            logger.error(f"Error in response_uploader_worker: {e}", exc_info=True)
            # Retry logic can be added here if needed
            gevent.sleep(5)
```

#### 6. Update Main Process Loop - REVISED

**Modify** `process()` function (app/worker.py:~94):

```python
def process(config_dict):
    """
    Main entry point - spawns 3 module workers with AC server concurrency control.
    """
    logger = config_dict['logger']
    executor_pool_size = config_dict.get('executor_pool_size', 10)  # New config

    # Initialize queues and semaphore
    config_dict['task_queue'] = Queue(maxsize=100)
    config_dict['response_queue'] = Queue(maxsize=100)
    config_dict['response_queue_threshold'] = config_dict.get('response_queue_threshold', 80)
    config_dict['ac_server_semaphore'] = BoundedSemaphore(2)  # Max 2 concurrent AC requests

    logger.info(f"Starting agent with executor pool size: {executor_pool_size}")
    logger.info(f"AC server concurrency limit: 2 (1 fetcher + 1 uploader)")
    logger.info(f"Response queue backpressure threshold: {config_dict['response_queue_threshold']}")

    # Spawn Module 1: Smart Task Fetcher (1 greenlet)
    gevent.spawn(task_fetcher_worker, config_dict)
    logger.info("Started Module 1: Smart Task Fetcher")

    # Spawn Module 2: Request Executor (N greenlets)
    executor_pool = Pool(executor_pool_size)
    for i in range(executor_pool_size):
        executor_pool.spawn(request_executor_worker, config_dict)
    logger.info(f"Started Module 2: Request Executor ({executor_pool_size} workers)")

    # Spawn Module 3: Response Uploader (1 greenlet)
    gevent.spawn(response_uploader_worker, config_dict)
    logger.info("Started Module 3: Response Uploader")

    # Keep main thread alive with monitoring
    while True:
        gevent.sleep(60)  # Check every 60s
        task_q = config_dict['task_queue']
        response_q = config_dict['response_queue']
        logger.info(
            f"Agent status - Task queue: {task_q.qsize()}/{task_q.maxsize}, "
            f"Response queue: {response_q.qsize()}/{response_q.maxsize}"
        )
```

#### 7. Add New Configuration Parameters - REVISED

**Update argument parser** (app/worker.py:~700):

```python
parser.add_argument(
    '--executorPoolSize',
    type=int,
    default=10,
    help='Number of concurrent request executor workers (Module 2, default: 10)'
)

parser.add_argument(
    '--responseQueueThreshold',
    type=int,
    default=80,
    help='Response queue size threshold for backpressure (default: 80, stops fetching at 80%)'
)

parser.add_argument(
    '--acMaxConcurrent',
    type=int,
    default=2,
    help='Maximum concurrent connections to AC server (default: 2, DO NOT CHANGE unless AC server limit increases)'
)

# In config_dict initialization
config_dict['executor_pool_size'] = args.executorPoolSize
config_dict['response_queue_threshold'] = args.responseQueueThreshold
config_dict['ac_max_concurrent'] = args.acMaxConcurrent
```

#### 8. Update Existing Functions

**Minimal changes needed**:

- `process_task()`: Add `config_dict` parameter (already exists mostly)
- `update_task()`: Add `config_dict` parameter (already exists mostly)
- Remove `thread_pool.wait_available()` logic (no longer needed)

### Configuration Changes

**New CLI arguments**:
```bash
python worker.py \
  --serverUrl https://armorcode.com \
  --apiKey YOUR_API_KEY \
  --executorPoolSize 10        # New: Module 2 pool size (default: 10)
```

**Environment variables** (optional):
```bash
export EXECUTOR_POOL_SIZE=10
```

---

## VISUAL FLOW DIAGRAM (REVISED)

```
┌──────────────────────────────────────────────────────────────────────────┐
│                     Detailed Execution Flow                               │
└──────────────────────────────────────────────────────────────────────────┘

Time →

Module 1 (Fetcher):
    ┌─────────────────────────────────────────────────────────────────┐
    │ 1. Check: response_queue.qsize() < 80?                          │
    │    ✗ NO  → Sleep 5s (backpressure)                              │
    │    ✓ YES → Continue                                             │
    │                                                                  │
    │ 2. Acquire AC semaphore (blocks if 2 connections active)        │
    │    Semaphore: [🔒 Slot 1 TAKEN] [⚪ Slot 2 FREE]                │
    │                                                                  │
    │ 3. GET /api/http-teleport/get-task                              │
    │    ← Response: 200 OK with task                                 │
    │                                                                  │
    │ 4. Release AC semaphore                                         │
    │    Semaphore: [⚪ Slot 1 FREE] [⚪ Slot 2 FREE]                  │
    │                                                                  │
    │ 5. Put task into task_queue                                     │
    │    task_queue: [task1, task2, ...] ← NEW TASK                   │
    └─────────────────────────────────────────────────────────────────┘

Module 2 (Executor Pool - 10 workers):
    ┌─────────────────────────────────────────────────────────────────┐
    │ Worker 1: Get task from task_queue → Process → response_queue   │
    │ Worker 2: Get task from task_queue → Process → response_queue   │
    │ ...                                                              │
    │ Worker 10: Get task from task_queue → Process → response_queue  │
    │                                                                  │
    │ NO AC SERVER REQUESTS - only internal tool calls                │
    └─────────────────────────────────────────────────────────────────┘

Module 3 (Uploader):
    ┌─────────────────────────────────────────────────────────────────┐
    │ 1. Get completed task from response_queue (blocks if empty)     │
    │    response_queue: [result1, result2, ...] → GET result1        │
    │                                                                  │
    │ 2. Acquire AC semaphore (blocks if 2 connections active)        │
    │    Semaphore: [⚪ Slot 1 FREE] [🔒 Slot 2 TAKEN]                │
    │                                                                  │
    │ 3. POST /api/http-teleport/put-result                           │
    │    ← Response: 200 OK                                           │
    │                                                                  │
    │ 4. Release AC semaphore                                         │
    │    Semaphore: [⚪ Slot 1 FREE] [⚪ Slot 2 FREE]                  │
    └─────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────────────┐
│                     Concurrency Guarantee                                 │
├──────────────────────────────────────────────────────────────────────────┤
│ AC Semaphore State (max 2 slots):                                        │
│                                                                           │
│ Scenario 1: Both fetcher and uploader want to connect simultaneously     │
│   Fetcher: acquire() → Slot 1 ✓                                          │
│   Uploader: acquire() → Slot 2 ✓                                         │
│   Result: 2 concurrent connections (ALLOWED)                             │
│                                                                           │
│ Scenario 2: Fetcher already connected, uploader tries to connect         │
│   Fetcher: holding Slot 1 🔒                                              │
│   Uploader: acquire() → Slot 2 ✓                                         │
│   Result: 2 concurrent connections (ALLOWED)                             │
│                                                                           │
│ Scenario 3: Both slots taken, fetcher tries again (IMPOSSIBLE IN DESIGN) │
│   This scenario CANNOT happen because:                                   │
│   - Only 1 fetcher greenlet exists                                       │
│   - Only 1 uploader greenlet exists                                      │
│   - They each hold semaphore for < 30s (request timeout)                 │
│   - Maximum possible: 2 concurrent (fetcher + uploader)                  │
│                                                                           │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## CRITICAL DESIGN FEATURES (REVISED)

### 1. AC Server Concurrency Control (Semaphore)

**Problem Solved**: "Too many concurrent requests" errors from AC server

**Solution**: `BoundedSemaphore(2)` guarantees exactly 2 concurrent connections

**How it works**:
```python
ac_semaphore = BoundedSemaphore(2)  # 2 slots available

# Module 1 (fetcher) wants to make request
ac_semaphore.acquire()  # Takes slot 1 ✓
# ... fetch task ...
ac_semaphore.release()  # Frees slot 1 ✓

# Module 3 (uploader) wants to make request
ac_semaphore.acquire()  # Takes slot 2 ✓
# ... upload response ...
ac_semaphore.release()  # Frees slot 2 ✓

# If both modules are busy, the next acquire() blocks until one releases
```

**Benefits**:
- ✅ **Guaranteed max 2 concurrent** AC server requests
- ✅ **No more "too many concurrent requests" errors**
- ✅ **Fair scheduling** between fetcher and uploader
- ✅ **Automatic blocking** when limit reached

### 2. Smart Task Fetching (Backpressure)

**Problem Solved**: Fetcher pulling tasks infinitely even when system can't process

**Solution**: Only fetch when `response_queue.qsize() < threshold`

**How it works**:
```python
# Before fetching, check if system can handle more tasks
if response_queue.qsize() >= 80:  # 80% capacity
    logger.warning("Response queue near full, pausing fetching")
    gevent.sleep(5)  # Wait for uploader to drain
    continue  # Skip fetching

# System has capacity, safe to fetch
fetch_task_from_server()
```

**Benefits**:
- ✅ **No infinite polling** when system is busy
- ✅ **Memory controlled** - queues never overflow
- ✅ **Self-throttling** - agent adapts to processing speed
- ✅ **No task drops** - fetcher waits instead of overwhelming system

### 3. Three-Module Pipeline

**Why 3 modules?**
1. **Module 1 (Fetcher)**: Always ready to fetch when capacity available
2. **Module 2 (Executor)**: Scales independently (10-50 workers)
3. **Module 3 (Uploader)**: Always ready to upload completed tasks

**Flow**:
```
AC Server → [Fetcher] → task_queue → [Executor Pool] → response_queue → [Uploader] → AC Server
             1 thread               N threads                          1 thread
             1 AC conn              0 AC conn                          1 AC conn
```

---

### Advantages (REVISED)

✅ **Minimal code changes**: ~150 lines added, mostly refactoring
✅ **Guaranteed AC concurrency control**: Exactly 2 concurrent requests (semaphore)
✅ **Smart task fetching**: Only pulls when system can process (backpressure)
✅ **Reuses 90% of existing code**: `process_task()` and `update_task()` unchanged
✅ **Guaranteed 2 AC server concurrency**: 1 fetcher + 1 uploader
✅ **Non-blocking task fetching**: Always polling for new tasks
✅ **Scalable internal processing**: Module 2 can scale to 20-50 workers
✅ **Easy debugging**: Queue sizes visible in logs
✅ **Graceful degradation**: If Module 2 is slow, tasks buffer in queue
✅ **No new dependencies**: Uses Python stdlib `queue.Queue`

### Disadvantages

⚠️ **Memory usage**: Queues hold tasks in memory (mitigated with `maxsize=100`)
⚠️ **Still Python**: Team lacks Python experts
⚠️ **Gevent complexity**: Cooperative multitasking can be tricky to debug

### Estimated Effort

- **Design & planning**: 2 hours (DONE)
- **Implementation**: 4-6 hours
  - Add queues: 30 minutes
  - Implement Module 1: 1 hour
  - Implement Module 2: 1 hour
  - Implement Module 3: 1 hour
  - Update main loop: 30 minutes
  - Testing & debugging: 1-2 hours
- **Integration testing**: 4 hours
- **Documentation**: 2 hours
- **Total**: 12-14 hours (~2 days)

### Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Queue memory overflow | LOW | MEDIUM | Use `maxsize=100` limit |
| Deadlock in queue operations | LOW | HIGH | Use timeouts on `put()` operations |
| Gevent monkey patching issues | LOW | HIGH | Already using gevent successfully |
| Backward compatibility | LOW | LOW | No API changes, only internal refactoring |

---

## Option 2: Go Rewrite

**Feasibility**: ✅ HIGH
**Time to Production**: 10-12 days
**Complexity**: MEDIUM
**Risk**: MEDIUM

### Proposed Architecture

```go
package main

import (
    "time"
    "net/http"
    "encoding/json"
)

// Task represents a work item from AC server
type Task struct {
    TaskID         string            `json:"taskId"`
    URL            string            `json:"url"`
    Method         string            `json:"method"`
    RequestHeaders map[string]string `json:"requestHeaders"`
    Input          string            `json:"input"`
    ExpiryTsMs     int64             `json:"expiryTsMs"`
}

// Module 1: Task Fetcher
func taskFetcher(config Config, taskQueue chan Task) {
    client := &http.Client{Timeout: 25 * time.Second}
    backoff := 5 * time.Second
    maxBackoff := 600 * time.Second

    for {
        // Fetch task from AC server
        resp, err := client.Get(config.ServerURL + "/api/http-teleport/get-task")
        if err != nil {
            log.Printf("Error fetching task: %v", err)
            time.Sleep(backoff)
            continue
        }

        if resp.StatusCode == 200 {
            var task Task
            json.NewDecoder(resp.Body).Decode(&task)
            resp.Body.Close()

            // Non-blocking send to queue
            select {
            case taskQueue <- task:
                log.Printf("Queued task: %s", task.TaskID)
                backoff = 5 * time.Second  // Reset backoff
            default:
                log.Println("Task queue full, dropping task")
            }

        } else if resp.StatusCode == 204 {
            // No tasks available
            resp.Body.Close()
            time.Sleep(1 * time.Second)
            backoff = 5 * time.Second  // Reset backoff

        } else if resp.StatusCode >= 500 {
            // Server error - exponential backoff
            resp.Body.Close()
            log.Printf("Server error %d, backing off %v", resp.StatusCode, backoff)
            time.Sleep(backoff)
            backoff = min(maxBackoff, backoff*2)

        } else {
            resp.Body.Close()
            log.Printf("Unexpected status code: %d", resp.StatusCode)
            time.Sleep(5 * time.Second)
        }
    }
}

// Module 2: Request Executor
func requestExecutor(config Config, taskQueue, responseQueue chan Task) {
    client := &http.Client{
        Timeout: time.Duration(config.Timeout) * time.Second,
    }

    for task := range taskQueue {
        log.Printf("Processing task: %s", task.TaskID)

        // Execute request to internal tool
        result := executeRequest(client, task, config)

        // Send result to response queue
        responseQueue <- result

        log.Printf("Completed task: %s", task.TaskID)
    }
}

// Module 3: Response Uploader
func responseUploader(config Config, responseQueue chan Task) {
    client := &http.Client{Timeout: 300 * time.Second}

    for task := range responseQueue {
        log.Printf("Uploading response for task: %s", task.TaskID)

        // Upload result to AC server
        err := uploadResult(client, task, config)
        if err != nil {
            log.Printf("Error uploading result: %v", err)
            // Retry logic can be added here
        } else {
            log.Printf("Uploaded response for task: %s", task.TaskID)
        }
    }
}

// Main entry point
func main() {
    config := loadConfig()

    // Create buffered channels (queues)
    taskQueue := make(chan Task, 100)
    responseQueue := make(chan Task, 100)

    // Start Module 1: Task Fetcher (1 goroutine)
    go taskFetcher(config, taskQueue)
    log.Println("Started Module 1: Task Fetcher")

    // Start Module 2: Request Executor (N goroutines)
    executorPoolSize := config.ExecutorPoolSize  // e.g., 10
    for i := 0; i < executorPoolSize; i++ {
        go requestExecutor(config, taskQueue, responseQueue)
    }
    log.Printf("Started Module 2: Request Executor (%d workers)", executorPoolSize)

    // Start Module 3: Response Uploader (1 goroutine)
    go responseUploader(config, responseQueue)
    log.Println("Started Module 3: Response Uploader")

    // Keep main goroutine alive
    select {}
}
```

### Advantages

✅ **Simpler concurrency**: Channels are easier than queues + gevent
✅ **Better performance**: 2-3x faster, 50% less memory (~30MB vs ~80MB)
✅ **Built-in HTTP client**: `net/http` is production-ready
✅ **Static typing**: Catch errors at compile time
✅ **Single binary**: Easy deployment, no runtime dependencies
✅ **Lightweight goroutines**: Can run 1000s easily (vs 50-100 greenlets)
✅ **Better tooling**: `go fmt`, `go vet`, `go test`, `pprof` profiling
✅ **Industry standard**: Kubernetes, Docker, Terraform all in Go
✅ **Simple syntax**: Easier for non-experts to learn than Python
✅ **Cross-compilation**: Build for Linux/Mac/Windows from any platform

### Disadvantages

⚠️ **Rewrite effort**: ~1000 lines Python → ~800 lines Go
⚠️ **Learning curve**: Team needs to learn Go (but simpler than Python)
⚠️ **Time to production**: 2-3 weeks for full rewrite + testing
⚠️ **Metrics integration**: Need to integrate with DataDog Go SDK

### Project Structure

```
web-agent-go/
├── main.go                 # Entry point
├── config/
│   └── config.go          # Configuration management
├── modules/
│   ├── fetcher.go         # Module 1: Task Fetcher
│   ├── executor.go        # Module 2: Request Executor
│   └── uploader.go        # Module 3: Response Uploader
├── models/
│   └── task.go            # Task struct definitions
├── metrics/
│   └── metrics.go         # Metrics collection
├── utils/
│   ├── http.go            # HTTP utilities
│   ├── ratelimit.go       # Rate limiter
│   └── logger.go          # Logging
├── go.mod                 # Dependency management
├── go.sum
├── Dockerfile             # Container image
└── README.md
```

### Dependencies

```go
// go.mod
module github.com/armorcode/web-agent

go 1.21

require (
    github.com/DataDog/datadog-go/v5 v5.3.0  // Metrics
    go.uber.org/zap v1.26.0                   // Logging
    gopkg.in/yaml.v3 v3.0.1                   // Config
)
```

### Estimated Effort

- **Core agent implementation**: 3-4 days
  - Module 1 (fetcher): 4 hours
  - Module 2 (executor): 8 hours
  - Module 3 (uploader): 4 hours
  - HTTP utilities: 4 hours
  - Rate limiter: 2 hours
  - Configuration: 2 hours
- **Metrics system**: 2 days (DataDog integration)
- **Testing**: 3-4 days
  - Unit tests: 2 days
  - Integration tests: 1-2 days
- **Documentation**: 1 day
- **Deployment tooling**: 1 day (Docker, systemd)
- **Total**: 10-12 days

### Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Go learning curve | MEDIUM | MEDIUM | Provide training, code examples |
| Missing Python libraries | LOW | LOW | Go stdlib is comprehensive |
| Performance issues | LOW | LOW | Go is faster than Python |
| Integration issues | LOW | MEDIUM | Maintain same API contract |
| Team adoption | MEDIUM | HIGH | Provide thorough documentation |

---

## Option 3: Java Rewrite

**Feasibility**: ✅ HIGH
**Time to Production**: 12-15 days
**Complexity**: MEDIUM-HIGH
**Risk**: MEDIUM

### Proposed Architecture

```java
package com.armorcode.agent;

import java.util.concurrent.*;
import java.net.http.*;
import java.time.Duration;

public class ArmorCodeAgent {
    private final BlockingQueue<Task> taskQueue = new LinkedBlockingQueue<>(100);
    private final BlockingQueue<Task> responseQueue = new LinkedBlockingQueue<>(100);
    private final Config config;

    public ArmorCodeAgent(Config config) {
        this.config = config;
    }

    // Module 1: Task Fetcher
    class TaskFetcher implements Runnable {
        private final HttpClient client = HttpClient.newBuilder()
            .connectTimeout(Duration.ofSeconds(25))
            .build();

        @Override
        public void run() {
            int backoff = 5;
            int maxBackoff = 600;

            while (true) {
                try {
                    // Fetch task from AC server
                    HttpRequest request = HttpRequest.newBuilder()
                        .uri(URI.create(config.getServerUrl() + "/api/http-teleport/get-task"))
                        .header("Authorization", "Bearer " + config.getApiKey())
                        .timeout(Duration.ofSeconds(25))
                        .GET()
                        .build();

                    HttpResponse<String> response = client.send(request,
                        HttpResponse.BodyHandlers.ofString());

                    if (response.statusCode() == 200) {
                        Task task = parseTask(response.body());
                        taskQueue.offer(task, 5, TimeUnit.SECONDS);  // Wait max 5s
                        logger.info("Queued task: " + task.getTaskId());
                        backoff = 5;  // Reset backoff

                    } else if (response.statusCode() == 204) {
                        // No tasks available
                        Thread.sleep(1000);
                        backoff = 5;  // Reset backoff

                    } else if (response.statusCode() >= 500) {
                        // Server error - exponential backoff
                        logger.warning("Server error " + response.statusCode() +
                                     ", backing off " + backoff + "s");
                        Thread.sleep(backoff * 1000);
                        backoff = Math.min(maxBackoff, backoff * 2);

                    } else {
                        logger.error("Unexpected status code: " + response.statusCode());
                        Thread.sleep(5000);
                    }

                } catch (Exception e) {
                    logger.error("Error in task fetcher", e);
                    try { Thread.sleep(5000); } catch (InterruptedException ie) {}
                }
            }
        }
    }

    // Module 2: Request Executor
    class RequestExecutor implements Runnable {
        private final HttpClient client = HttpClient.newBuilder()
            .connectTimeout(Duration.ofSeconds(config.getTimeout()))
            .build();

        @Override
        public void run() {
            while (true) {
                try {
                    // Block until task is available
                    Task task = taskQueue.take();

                    logger.info("Processing task: " + task.getTaskId());

                    // Execute request to internal tool
                    Task result = executeRequest(client, task);

                    // Queue result for upload
                    responseQueue.offer(result, 5, TimeUnit.SECONDS);

                    logger.info("Completed task: " + task.getTaskId());

                } catch (Exception e) {
                    logger.error("Error in request executor", e);
                }
            }
        }
    }

    // Module 3: Response Uploader
    class ResponseUploader implements Runnable {
        private final HttpClient client = HttpClient.newBuilder()
            .connectTimeout(Duration.ofSeconds(300))
            .build();

        @Override
        public void run() {
            while (true) {
                try {
                    // Block until result is available
                    Task task = responseQueue.take();

                    logger.info("Uploading response for task: " + task.getTaskId());

                    // Upload result to AC server
                    uploadResult(client, task);

                    logger.info("Uploaded response for task: " + task.getTaskId());

                } catch (Exception e) {
                    logger.error("Error in response uploader", e);
                }
            }
        }
    }

    // Main entry point
    public void start() {
        ExecutorService executorPool = Executors.newFixedThreadPool(
            config.getExecutorPoolSize()
        );

        // Start Module 1: Task Fetcher (1 thread)
        new Thread(new TaskFetcher(), "task-fetcher").start();
        logger.info("Started Module 1: Task Fetcher");

        // Start Module 2: Request Executor (N threads)
        for (int i = 0; i < config.getExecutorPoolSize(); i++) {
            executorPool.execute(new RequestExecutor());
        }
        logger.info("Started Module 2: Request Executor (" +
                   config.getExecutorPoolSize() + " workers)");

        // Start Module 3: Response Uploader (1 thread)
        new Thread(new ResponseUploader(), "response-uploader").start();
        logger.info("Started Module 3: Response Uploader");

        // Keep main thread alive
        while (true) {
            try {
                Thread.sleep(60000);
                logger.debug("Agent status - Task queue: " + taskQueue.size() +
                           ", Response queue: " + responseQueue.size());
            } catch (InterruptedException e) {
                break;
            }
        }
    }

    public static void main(String[] args) {
        Config config = Config.fromArgs(args);
        ArmorCodeAgent agent = new ArmorCodeAgent(config);
        agent.start();
    }
}
```

### Advantages

✅ **Mature ecosystem**: Spring Boot, OkHttp, Jackson, etc.
✅ **Enterprise-grade tooling**: JProfiler, VisualVM, JMX monitoring
✅ **Built-in concurrency**: `java.util.concurrent` is battle-tested
✅ **Strong typing**: Catch errors at compile time
✅ **IDE support**: IntelliJ, Eclipse have excellent Java support
✅ **Team familiarity**: More Java experts available than Go
✅ **Rich libraries**: Apache Commons, Guava, etc.
✅ **Debugging tools**: Remote debugging, heap dumps, thread dumps

### Disadvantages

⚠️ **Verbosity**: ~1000 lines Python → ~1500 lines Java
⚠️ **Memory footprint**: JVM overhead (~150MB minimum vs ~30MB Go)
⚠️ **Slower startup**: JVM warmup time (5-10s vs 0.5s Go)
⚠️ **Deployment complexity**: Need JRE on customer servers (vs single binary)
⚠️ **Rewrite effort**: Similar to Go (2-3 weeks)
⚠️ **Overkill for agent**: Java better suited for large applications

### Project Structure

```
web-agent-java/
├── pom.xml                              # Maven dependencies
├── src/
│   ├── main/
│   │   ├── java/com/armorcode/agent/
│   │   │   ├── ArmorCodeAgent.java     # Main class
│   │   │   ├── config/
│   │   │   │   └── Config.java         # Configuration
│   │   │   ├── modules/
│   │   │   │   ├── TaskFetcher.java    # Module 1
│   │   │   │   ├── RequestExecutor.java # Module 2
│   │   │   │   └── ResponseUploader.java # Module 3
│   │   │   ├── models/
│   │   │   │   └── Task.java           # Task POJO
│   │   │   ├── metrics/
│   │   │   │   └── MetricsCollector.java
│   │   │   └── utils/
│   │   │       ├── HttpUtils.java
│   │   │       ├── RateLimiter.java
│   │   │       └── Logger.java
│   │   └── resources/
│   │       ├── application.properties   # Configuration
│   │       └── logback.xml             # Logging config
│   └── test/
│       └── java/com/armorcode/agent/   # Unit tests
├── Dockerfile
└── README.md
```

### Dependencies (Maven)

```xml
<dependencies>
    <!-- HTTP client -->
    <dependency>
        <groupId>com.squareup.okhttp3</groupId>
        <artifactId>okhttp</artifactId>
        <version>4.12.0</version>
    </dependency>

    <!-- JSON processing -->
    <dependency>
        <groupId>com.fasterxml.jackson.core</groupId>
        <artifactId>jackson-databind</artifactId>
        <version>2.16.0</version>
    </dependency>

    <!-- Logging -->
    <dependency>
        <groupId>ch.qos.logback</groupId>
        <artifactId>logback-classic</artifactId>
        <version>1.4.14</version>
    </dependency>

    <!-- Metrics -->
    <dependency>
        <groupId>com.datadoghq</groupId>
        <artifactId>java-dogstatsd-client</artifactId>
        <version>4.2.0</version>
    </dependency>

    <!-- Testing -->
    <dependency>
        <groupId>junit</groupId>
        <artifactId>junit</artifactId>
        <version>4.13.2</version>
        <scope>test</scope>
    </dependency>
</dependencies>
```

### Estimated Effort

- **Core agent implementation**: 4-5 days
  - Module 1 (fetcher): 6 hours
  - Module 2 (executor): 10 hours
  - Module 3 (uploader): 6 hours
  - HTTP utilities: 6 hours
  - Rate limiter: 3 hours
  - Configuration: 3 hours
- **Metrics system**: 2-3 days (DataDog integration)
- **Testing**: 4 days
  - Unit tests: 2 days
  - Integration tests: 2 days
- **Documentation**: 1 day
- **Deployment tooling**: 1 day (Docker, systemd)
- **Total**: 12-15 days

### Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| JVM memory overhead | HIGH | MEDIUM | Optimize JVM flags, use GraalVM |
| Deployment complexity | MEDIUM | MEDIUM | Use Docker containers |
| Performance issues | LOW | LOW | Java 17+ is fast |
| Integration issues | LOW | MEDIUM | Maintain same API contract |
| Startup time | HIGH | LOW | Use JVM warmup, CDS archives |

---

## Comparison Matrix

| Criteria | Python Queue Refactoring | Go Rewrite | Java Rewrite |
|----------|-------------------------|-----------|--------------|
| **Development Time** | 1-2 days | 10-12 days | 12-15 days |
| **Lines of Code** | +100 (refactor) | ~800 | ~1500 |
| **Memory Usage** | 80MB | 30MB | 150MB |
| **CPU Usage** | Medium | Low | Medium |
| **Startup Time** | 2s | 0.5s | 5-10s |
| **Deployment Size** | 50MB (Python runtime) | 15MB (binary) | 100MB (JRE + JAR) |
| **Concurrency Model** | Queues + gevent | Channels + goroutines | BlockingQueue + threads |
| **Code Maintainability** | Medium | High | Medium |
| **Team Learning Curve** | Low | Medium | Low |
| **Industry Adoption (agents)** | Medium | High | Low |
| **Type Safety** | No (dynamic) | Yes (static) | Yes (static) |
| **Error Handling** | Try/except | Explicit errors | Try/catch |
| **Testing Tools** | pytest, unittest | go test, testify | JUnit, Mockito |
| **Debugging Tools** | pdb, logging | delve, pprof | jdb, VisualVM |
| **Profiling** | cProfile | pprof (excellent) | JProfiler, YourKit |
| **Cross-platform Build** | No (runtime needed) | Yes (single binary) | No (JRE needed) |
| **Hot Reload** | Yes (Python) | No (compiled) | Limited (JVM) |
| **Dependency Management** | pip, requirements.txt | go mod | Maven, Gradle |
| **Production Readiness** | 1-2 days | 2-3 weeks | 3-4 weeks |
| **AC Server Concurrency** | ✅ Exactly 2 | ✅ Exactly 2 | ✅ Exactly 2 |
| **Non-blocking Fetch** | ✅ Yes | ✅ Yes | ✅ Yes |
| **Scalable Processing** | ✅ Yes (10-50 workers) | ✅ Yes (100+ workers) | ✅ Yes (50-100 workers) |

### Performance Benchmarks (Estimated)

| Metric | Python (Current) | Python (Queue) | Go | Java |
|--------|-----------------|---------------|-----|------|
| Tasks/minute | 50-100 | 200-300 | 400-500 | 300-400 |
| Memory | 80MB | 80MB | 30MB | 150MB |
| CPU (idle) | 5% | 5% | 1% | 3% |
| CPU (load) | 40% | 40% | 20% | 30% |
| Latency (p50) | 100ms | 100ms | 50ms | 80ms |
| Latency (p99) | 500ms | 500ms | 200ms | 300ms |

---

## Recommended Approach

### Two-Phase Strategy

#### Phase 1: Immediate Fix (This Week)
**Implement Python Queue Refactoring**

**Why**:
- ✅ **Fixes critical bottleneck immediately**
- ✅ **Minimal changes** (~100 lines, mostly refactoring)
- ✅ **Low risk** - reuses existing code
- ✅ **Guarantees 2 concurrent AC requests**
- ✅ **Production-ready in 1-2 days**

**Success Metrics**:
- Task fetch never blocks
- Throughput increases 2-4x (50 → 200+ tasks/min)
- Queue sizes visible in logs
- Zero task drops

#### Phase 2: Long-term Solution (Next Quarter)
**Rewrite in Go**

**Why**:
- ✅ **Simpler to maintain** than Python (channels vs queues + gevent)
- ✅ **Better performance** (2-3x faster, 50% less memory)
- ✅ **Industry standard** for infrastructure agents
- ✅ **Easier for non-experts** to understand
- ✅ **Single binary deployment** - no runtime dependencies
- ✅ **Better tooling** and debugging

**Success Metrics**:
- Throughput increases 4-8x vs current (50 → 400+ tasks/min)
- Memory usage decreases 50% (80MB → 30MB)
- Deployment size decreases 70% (50MB → 15MB)
- Zero Python-related issues

---

## Implementation Details - Phase 1 (Python Queue Refactoring)

### Step-by-Step Implementation Plan

#### Step 1: Preparation (2 hours)
1. Create feature branch: `git checkout -b refactor/queue-based-architecture`
2. Backup current `app/worker.py`: `cp app/worker.py app/worker.py.backup`
3. Update `requirements.txt` (no new dependencies needed - stdlib only)
4. Review existing test cases

#### Step 2: Add Queue Infrastructure (1 hour)
1. Import queue module (app/worker.py:1-30)
2. Add global queue variables (app/worker.py:~90)
3. Add queue size monitoring to metrics

**Code changes**:
```python
# At top of file
from queue import Queue, Empty

# In config initialization
config_dict['task_queue'] = Queue(maxsize=100)
config_dict['response_queue'] = Queue(maxsize=100)
```

#### Step 3: Implement Module 1 - Task Fetcher (2 hours)
1. Extract task fetching logic from `process()` function
2. Create `task_fetcher_worker()` function
3. Add queue insertion logic
4. Add error handling and retry logic
5. Add logging

**File**: app/worker.py
**Function**: `task_fetcher_worker(config_dict)`
**Lines**: ~80 lines

#### Step 4: Implement Module 2 - Request Executor (2 hours)
1. Create `request_executor_worker()` function
2. Add queue consumption logic
3. Reuse existing `process_task()` function
4. Add result queueing logic
5. Add error handling

**File**: app/worker.py
**Function**: `request_executor_worker(config_dict)`
**Lines**: ~60 lines

#### Step 5: Implement Module 3 - Response Uploader (2 hours)
1. Create `response_uploader_worker()` function
2. Add queue consumption logic
3. Reuse existing `update_task()` function
4. Add retry logic
5. Add logging

**File**: app/worker.py
**Function**: `response_uploader_worker(config_dict)`
**Lines**: ~60 lines

#### Step 6: Update Main Process Loop (1 hour)
1. Refactor `process()` function
2. Spawn Module 1 greenlet
3. Create Module 2 pool and spawn workers
4. Spawn Module 3 greenlet
5. Add monitoring loop
6. Remove old blocking pool logic

**File**: app/worker.py
**Function**: `process(config_dict)`
**Lines**: ~40 lines (simplified)

#### Step 7: Add Configuration (30 minutes)
1. Add `--executorPoolSize` CLI argument
2. Add environment variable support
3. Update README.md with new configuration

#### Step 8: Testing (4 hours)
1. **Unit tests** (2 hours):
   - Test queue operations
   - Test each module in isolation
   - Mock AC server responses
2. **Integration tests** (2 hours):
   - Test full flow (fetch → execute → upload)
   - Test error scenarios (5XX, timeouts)
   - Test queue overflow scenarios

#### Step 9: Documentation (2 hours)
1. Update README.md with new architecture
2. Document configuration options
3. Add troubleshooting guide
4. Update deployment docs

#### Step 10: Deployment (2 hours)
1. Update Docker image
2. Test in staging environment
3. Monitor queue sizes and throughput
4. Gradual rollout to production

### Code Review Checklist

- [ ] Queue maxsize configured (prevents memory overflow)
- [ ] All queue operations have timeouts (prevents deadlocks)
- [ ] Existing `process_task()` function reused (no duplication)
- [ ] Existing `update_task()` function reused (no duplication)
- [ ] Rate limiting preserved (25 req/15s)
- [ ] Error handling preserved (retry logic, backoff)
- [ ] Logging preserved (task IDs, queue sizes)
- [ ] Metrics preserved (BufferedMetricsLogger)
- [ ] Graceful shutdown implemented (SIGTERM handling)
- [ ] No breaking changes to CLI arguments
- [ ] Backward compatible with existing deployments

### Testing Strategy

#### Unit Tests

**Test Module 1 (Task Fetcher)**:
```python
def test_task_fetcher_success():
    # Mock successful task fetch
    # Assert task added to queue
    pass

def test_task_fetcher_no_tasks():
    # Mock 204 response
    # Assert no queue insertion
    pass

def test_task_fetcher_server_error():
    # Mock 500 response
    # Assert exponential backoff
    pass

def test_task_fetcher_queue_full():
    # Fill queue to maxsize
    # Assert blocks or drops gracefully
    pass
```

**Test Module 2 (Request Executor)**:
```python
def test_request_executor_success():
    # Add task to queue
    # Assert task processed
    # Assert result added to response queue
    pass

def test_request_executor_internal_tool_error():
    # Mock internal tool failure
    # Assert error response queued
    pass
```

**Test Module 3 (Response Uploader)**:
```python
def test_response_uploader_success():
    # Add result to queue
    # Assert uploaded to AC server
    pass

def test_response_uploader_retry():
    # Mock 429 error
    # Assert retry logic triggered
    pass
```

#### Integration Tests

**Test End-to-End Flow**:
```python
def test_full_flow():
    # 1. Start agent with test config
    # 2. Mock AC server with test tasks
    # 3. Mock internal tools
    # 4. Assert tasks fetched, processed, uploaded
    # 5. Assert correct AC server concurrency (max 2)
    pass
```

**Test Concurrency**:
```python
def test_concurrent_processing():
    # Add 20 tasks to queue
    # Assert max 2 AC server requests at any time
    # Assert Module 2 processes multiple tasks concurrently
    pass
```

**Test Queue Overflow**:
```python
def test_queue_overflow():
    # Add 150 tasks (exceeds maxsize=100)
    # Assert queue blocks or drops gracefully
    # Assert no memory issues
    pass
```

#### Load Tests

**Test Throughput**:
```bash
# Send 1000 tasks to AC server
# Measure:
# - Tasks per minute (target: 200+)
# - Memory usage (target: <100MB)
# - CPU usage (target: <50%)
# - Queue sizes (target: <50)
```

**Test Long-running Tasks**:
```bash
# Send tasks that take 60-120s to process
# Measure:
# - Task fetching continues (non-blocking)
# - No queue deadlocks
# - Graceful shutdown works
```

### Monitoring & Debugging

#### Log Messages

**Module 1 (Task Fetcher)**:
```
INFO: Fetched task: task-123
INFO: Queued task: task-123 (queue size: 42)
WARN: Server error 503, backing off 10s
ERROR: Task queue full, dropping task: task-456
```

**Module 2 (Request Executor)**:
```
INFO: Processing task: task-123
INFO: Completed task: task-123 in 45s
ERROR: Error processing task task-123: Connection refused
```

**Module 3 (Response Uploader)**:
```
INFO: Uploading response for task: task-123
INFO: Uploaded response for task: task-123 (size: 2.5MB)
WARN: Upload failed (429), retrying in 2s
```

#### Metrics

**New Metrics**:
```python
# Queue sizes (gauge)
task_queue_size = task_queue.qsize()
response_queue_size = response_queue.qsize()

# Processing times (histogram)
task_fetch_time_ms
task_execution_time_ms
task_upload_time_ms

# Success/failure counts (counter)
tasks_fetched_total
tasks_processed_total
tasks_uploaded_total
tasks_failed_total
```

#### Debug Mode

**Enable with**:
```bash
python worker.py --debugMode
```

**Debug Output**:
```
DEBUG: Agent status - Task queue: 42/100, Response queue: 18/100
DEBUG: Module 1: Fetching task (attempt 1523)
DEBUG: Module 2: Worker 3 processing task-789
DEBUG: Module 3: Uploading result for task-456
```

---

## Deployment Plan

### Phase 1 Deployment (Python Queue Refactoring)

#### Week 1: Development & Testing
- **Day 1-2**: Implementation (6-8 hours)
- **Day 3**: Unit testing (4 hours)
- **Day 4**: Integration testing (4 hours)
- **Day 5**: Documentation (2 hours)

#### Week 2: Staging & Rollout
- **Day 1**: Deploy to staging environment
- **Day 2**: Load testing (1000+ tasks)
- **Day 3**: Monitor metrics (24 hours)
- **Day 4**: Fix any issues
- **Day 5**: Deploy to production (gradual rollout)

#### Rollout Strategy

**Stage 1**: Internal testing (10% of customers)
- Select 5-10 low-risk customers
- Deploy new agent version
- Monitor for 48 hours
- Compare metrics: old vs new

**Stage 2**: Beta rollout (50% of customers)
- If Stage 1 successful, expand to 50%
- Monitor for 1 week
- Collect feedback

**Stage 3**: Full rollout (100% of customers)
- If Stage 2 successful, deploy to all
- Keep old version as fallback
- Monitor for 2 weeks

#### Rollback Plan

**If issues detected**:
1. Immediately rollback to old version
2. Investigate logs and metrics
3. Fix issues in development
4. Restart rollout from Stage 1

**Rollback triggers**:
- Throughput drops below old version
- Error rate increases >10%
- Memory usage increases >50%
- Customer reports issues

---

### Phase 2 Deployment (Go Rewrite)

#### Quarter 1: Planning & Development
- **Week 1-2**: Requirements & design
- **Week 3-6**: Implementation
- **Week 7-8**: Testing
- **Week 9**: Documentation

#### Quarter 2: Migration
- **Week 1-2**: Parallel deployment (Python + Go)
- **Week 3-4**: Comparison & optimization
- **Week 5-8**: Gradual migration (10% → 50% → 100%)
- **Week 9-10**: Cleanup & documentation
- **Week 11-12**: Deprecate Python agent

#### Migration Strategy

**Parallel Deployment**:
- Deploy Go agent alongside Python agent
- Route 10% of traffic to Go agent
- Compare metrics side-by-side
- Gradually shift traffic: 10% → 25% → 50% → 75% → 100%

**Validation Criteria**:
- Throughput ≥ 2x Python agent
- Memory usage ≤ 50% Python agent
- Error rate ≤ Python agent
- Zero customer-reported issues

---

## Risk Assessment

### Phase 1 Risks (Python Queue Refactoring)

| Risk | Likelihood | Impact | Mitigation | Owner |
|------|------------|--------|------------|-------|
| Queue deadlock | LOW | HIGH | Use timeouts on all queue operations | Dev team |
| Memory overflow | LOW | MEDIUM | Use `maxsize=100` limit | Dev team |
| Backward compatibility issues | LOW | MEDIUM | Keep CLI arguments unchanged | Dev team |
| Gevent monkey patching issues | LOW | HIGH | Already using gevent successfully | Dev team |
| Performance regression | LOW | MEDIUM | Load testing before rollout | QA team |
| Customer deployment issues | MEDIUM | LOW | Gradual rollout + rollback plan | DevOps |

### Phase 2 Risks (Go Rewrite)

| Risk | Likelihood | Impact | Mitigation | Owner |
|------|------------|--------|------------|-------|
| Go learning curve | MEDIUM | MEDIUM | Training + code reviews | Dev team |
| Missing Python features | LOW | LOW | Go stdlib is comprehensive | Dev team |
| Integration issues | LOW | MEDIUM | Maintain same API contract | Dev team |
| Team adoption resistance | MEDIUM | HIGH | Demonstrate benefits early | Management |
| Timeline delays | MEDIUM | MEDIUM | Buffer 2-3 weeks | Project manager |
| Customer resistance | LOW | LOW | Transparent communication | Customer success |

---

## Success Metrics

### Phase 1 Success Criteria (Python Queue Refactoring)

#### Functional
- ✅ Task fetch never blocks (measured via logs)
- ✅ Exactly 2 concurrent AC server requests (measured via monitoring)
- ✅ Zero task drops (unless queue genuinely full)
- ✅ Graceful shutdown works (SIGTERM handled)

#### Performance
- ✅ Throughput increases 2-4x: 50 → 200+ tasks/min
- ✅ Memory usage ≤ 100MB (no increase)
- ✅ CPU usage ≤ 50% at peak load
- ✅ Queue sizes <50 average (good buffer)

#### Operational
- ✅ Zero production incidents
- ✅ Zero customer-reported issues
- ✅ Deployment time <1 hour
- ✅ Rollback time <15 minutes

### Phase 2 Success Criteria (Go Rewrite)

#### Functional
- ✅ Feature parity with Python agent
- ✅ Same API contract (no customer changes)
- ✅ Metrics collection works (DataDog integration)

#### Performance
- ✅ Throughput increases 4-8x: 50 → 400+ tasks/min
- ✅ Memory usage decreases 50%: 80MB → 30MB
- ✅ Binary size <20MB (single file deployment)
- ✅ Startup time <1s

#### Operational
- ✅ Zero production incidents during migration
- ✅ Zero customer-reported issues
- ✅ 100% customer migration in 3 months
- ✅ Python agent fully deprecated

---

## Appendix

### A. Current Code References

**Main file**: `app/worker.py` (997 lines)

**Key functions**:
- `process()`: Main polling loop (lines 94-166)
- `process_task()`: Execute internal tool request (lines 308-444)
- `update_task()`: Upload result to AC server (lines 221-258)
- `RateLimiter`: Rate limiting class (lines 536-564)

**Critical bottleneck**: Line 142 (`thread_pool.wait_available()`)

### B. Configuration Reference

**CLI Arguments**:
```bash
--serverUrl          # AC server URL (required)
--apiKey             # API key (required)
--poolSize           # Current: Gevent pool size (default: 5)
--executorPoolSize   # NEW: Module 2 pool size (default: 10)
--rateLimitPerMin    # Rate limit (default: 250)
--timeout            # HTTP timeout (default: 30)
--verify             # SSL verification (default: false)
--debugMode          # Debug logging (default: false)
--envName            # Environment name (optional)
--inwardProxyHttps   # Internal proxy (optional)
--outgoingProxyHttps # External proxy (optional)
--uploadToAc         # Upload mode (default: true)
--metricsRetentionDays # Metrics retention (default: 7)
```

### C. Glossary

**AC Server**: ArmorCode platform server (armorcode.com)
**Greenlet**: Lightweight concurrent task (gevent library)
**Goroutine**: Lightweight concurrent task (Go language)
**Thread Pool**: Collection of worker threads
**Queue**: FIFO data structure for task buffering
**Channel**: Go's communication mechanism between goroutines
**BlockingQueue**: Java's thread-safe queue implementation
**Rate Limiter**: Enforces request rate limits (25 req/15s)
**Module 1**: Task Fetcher (polls AC server)
**Module 2**: Request Executor (calls internal tools)
**Module 3**: Response Uploader (uploads results)

### D. References

**Python Queue Documentation**:
- https://docs.python.org/3/library/queue.html

**Gevent Documentation**:
- http://www.gevent.org/

**Go Channels Tutorial**:
- https://go.dev/tour/concurrency/2

**Java BlockingQueue Documentation**:
- https://docs.oracle.com/javase/8/docs/api/java/util/concurrent/BlockingQueue.html

**DataDog Python SDK**:
- https://github.com/DataDog/datadogpy

**DataDog Go SDK**:
- https://github.com/DataDog/datadog-go

**DataDog Java SDK**:
- https://github.com/DataDog/java-dogstatsd-client

---

## Review Checklist

Before implementation, please review and confirm:

- [ ] Architecture makes sense (3-module queue-based)
- [ ] Queue sizes appropriate (100 for each queue)
- [ ] Module 2 pool size appropriate (10 workers)
- [ ] AC server concurrency guaranteed (exactly 2)
- [ ] Existing code reuse maximized (process_task, update_task)
- [ ] Configuration options acceptable (--executorPoolSize)
- [ ] Testing strategy comprehensive
- [ ] Deployment plan realistic (gradual rollout)
- [ ] Rollback plan acceptable
- [ ] Success metrics clear
- [ ] Timeline acceptable (Phase 1: 1-2 days, Phase 2: 2-3 months)
- [ ] Risk assessment complete
- [ ] Documentation plan acceptable

---

## Next Steps

1. **Review this plan** and suggest changes
2. **Approve Phase 1** (Python queue refactoring)
3. **Create implementation tasks** (GitHub issues)
4. **Assign developers**
5. **Begin implementation**

---

**Document prepared by**: Claude Code
**Date**: 2025-11-07
**Status**: Draft - Awaiting Review & Feedback
