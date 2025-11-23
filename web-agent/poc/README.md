# Agent POC - Queue-Based Architecture

This POC demonstrates the proposed 3-module queue-based architecture with:
- Smart backpressure control
- AC server concurrency limiting (max 2 concurrent)
- Thread-safe queue communication

---

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│              3-Module Queue Architecture                  │
├──────────────────────────────────────────────────────────┤
│                                                           │
│  ┌──────────┐      ┌──────────┐      ┌──────────┐       │
│  │ Module 1 │      │ Module 2 │      │ Module 3 │       │
│  │          │      │          │      │          │       │
│  │   Task   │─────▶│   Task   │─────▶│  Result  │       │
│  │ Fetcher  │queue │Processor │queue │ Uploader │       │
│  │          │  1   │   Pool   │  2   │          │       │
│  │(1 thread)│      │(N threads)      │(1 thread)│       │
│  └────┬─────┘      └──────────┘      └────┬─────┘       │
│       │                                     │             │
│       │     ┌──────────────────────┐       │             │
│       │     │  AC Server Semaphore │       │             │
│       └────▶│  (max 2 concurrent)  │◀──────┘             │
│             └──────────────────────┘                     │
│                                                           │
└──────────────────────────────────────────────────────────┘
```

---

## Components

### Mock Server (`mock_server.py`)
- **GET /get-task**: Returns random task with:
  - Random taskId (UUID)
  - Random task name (6 random characters)
  - Random iterations (5-15)
- **POST /complete-task**: Marks task as complete
- **GET /stats**: Server statistics

### Agent (`agent_poc.py`)
- **Module 1** (Task Fetcher): Polls server, implements backpressure
- **Module 2** (Task Processor Pool): Processes tasks (prints name every 1s)
- **Module 3** (Result Uploader): Uploads completed tasks

---

## Installation

### Requirements
```bash
pip install flask requests
```

Or using requirements file:
```bash
cd poc
pip install -r requirements.txt
```

---

## Usage

### Step 1: Start Mock Server

In **Terminal 1**:
```bash
cd poc
python mock_server.py
```

Output:
```
============================================================
Mock Server for Agent POC
============================================================
Endpoints:
  GET  /get-task       - Get a new random task
  POST /complete-task  - Mark task as complete
  GET  /stats          - Get server statistics
============================================================
Starting server on http://localhost:5000
============================================================
```

### Step 2: Start Agent

In **Terminal 2**:
```bash
cd poc
python agent_poc.py
```

Or with custom settings:
```bash
python agent_poc.py --pool-size 5 --threshold 80
```

Output:
```
============================================================
Starting Agent POC - 3-Module Architecture
============================================================
Server URL: http://localhost:5000
Processor Pool Size: 3
Response Queue Threshold: 80
AC Server Max Concurrent: 2
============================================================

Started 5 threads:
  - 1 Task Fetcher
  - 3 Task Processors
  - 1 Result Uploader

Agent running... Press Ctrl+C to stop
```

---

## Configuration Options

### Agent Options

```bash
python agent_poc.py [OPTIONS]

Options:
  --server URL          Mock server URL (default: http://localhost:5000)
  --pool-size N         Number of processor threads (default: 3)
  --threshold N         Response queue threshold for backpressure (default: 80)
```

### Examples

**Default settings** (3 processors, threshold 80):
```bash
python agent_poc.py
```

**High throughput** (10 processors):
```bash
python agent_poc.py --pool-size 10
```

**Conservative backpressure** (stop fetching at 50% queue):
```bash
python agent_poc.py --threshold 50
```

---

## Expected Behavior

### 1. Task Fetching with Backpressure

**When response queue < 80%**:
```
[MODULE 1] Fetching task from server...
[MODULE 1] Fetched task: abc123 - xyzabc (8 iterations)
[MODULE 1] Task queued. Queue size: 5
```

**When response queue ≥ 80%**:
```
[MODULE 1] Response queue near full (85/100), pausing fetching for 2s
```

### 2. Task Processing

```
[MODULE 2-1] Processing task: abc123 - xyzabc
[MODULE 2-1] Task xyzabc: iteration 1/8
[MODULE 2-1] Task xyzabc: iteration 2/8
...
[MODULE 2-1] Task xyzabc: iteration 8/8
[MODULE 2-1] Completed task: abc123 - xyzabc
```

### 3. Result Uploading

```
[MODULE 3] Uploading result for task: abc123 - xyzabc
[MODULE 3] Acquiring AC server semaphore...
[MODULE 3] Successfully completed task: abc123 - xyzabc
[MODULE 3] Released AC server semaphore
```

### 4. AC Server Concurrency Control

**Maximum 2 concurrent connections**:
- Module 1 (fetcher) holds 1 semaphore slot while fetching
- Module 3 (uploader) holds 1 semaphore slot while uploading
- If both are busy, the next operation blocks until a slot is free

---

## Statistics

Every 10 seconds, the agent prints statistics:

```
============================================================
Agent Statistics:
  Tasks Fetched:   25
  Tasks Processed: 20
  Tasks Completed: 18
  Task Queue:      5/100
  Response Queue:  2/100
============================================================
```

---

## Testing Scenarios

### Test 1: Verify Backpressure

1. Set low threshold: `python agent_poc.py --threshold 5`
2. Set high pool size: `python agent_poc.py --pool-size 1` (slow processing)
3. Observe: Fetcher pauses when response queue fills up

### Test 2: Verify AC Server Concurrency

1. Add logging around semaphore acquire/release
2. Check that max 2 "Acquiring" messages without "Released" in between
3. Never more than 2 concurrent server requests

### Test 3: Verify Queue Communication

1. Start agent with 3 processors
2. Observe tasks flowing: fetch → process → upload
3. Check queue sizes in statistics

### Test 4: Stress Test

1. High pool size: `python agent_poc.py --pool-size 20`
2. Run for 5 minutes
3. Check statistics: All fetched tasks should be completed

---

## Stopping the Agent

Press **Ctrl+C** in agent terminal:

```
^C
Shutting down agent...

============================================================
Agent Statistics:
  Tasks Fetched:   50
  Tasks Processed: 50
  Tasks Completed: 50
  Task Queue:      0/100
  Response Queue:  0/100
============================================================

Agent stopped.
```

---

## Key Features Demonstrated

### ✅ Smart Backpressure
- Fetcher monitors response queue size
- Stops fetching when threshold exceeded
- Self-throttling based on processing speed

### ✅ AC Server Concurrency Control
- BoundedSemaphore(2) guarantees max 2 concurrent connections
- One slot for fetcher, one for uploader
- Automatic blocking when limit reached

### ✅ 3-Module Pipeline
- **Module 1**: Always ready to fetch (when capacity available)
- **Module 2**: Scales independently (N workers)
- **Module 3**: Always ready to upload

### ✅ Thread-Safe Queues
- `Queue.put()` and `Queue.get()` are thread-safe
- Blocking behavior prevents race conditions
- Graceful shutdown with timeouts

---

## Differences from Production Agent

| Aspect | POC | Production Agent |
|--------|-----|------------------|
| Concurrency | `threading` | `gevent` (greenlets) |
| HTTP Client | `requests` | `requests` (same) |
| Task Work | Print every 1s | Call internal tools |
| File Handling | None | Stream to temp files |
| Base64 | None | Encode responses |
| Multipart Upload | None | Large file uploads |

**Note**: Core architecture is identical, just simpler task processing.

---

## Next Steps

After validating this POC:

1. **Apply to production agent** (`app/worker.py`)
2. **Add gevent integration** (replace threading with gevent)
3. **Add real task processing** (HTTP requests to internal tools)
4. **Add file handling** (streaming, base64, gzip)
5. **Add metrics** (BufferedMetricsLogger)

---

## Troubleshooting

### Server not accessible
```
[MODULE 1] Network error: Connection refused
```
**Solution**: Ensure mock server is running on port 5000

### Import errors
```
ModuleNotFoundError: No module named 'flask'
```
**Solution**: Install dependencies: `pip install flask requests`

### Port already in use
```
OSError: [Errno 48] Address already in use
```
**Solution**: Kill process on port 5000: `lsof -ti:5000 | xargs kill -9`

---

## Files

```
poc/
├── README.md           # This file
├── mock_server.py      # Mock AC server
├── agent_poc.py        # 3-module agent POC
└── requirements.txt    # Python dependencies
```

---

**Created**: 2025-11-07
**Purpose**: Validate queue-based architecture before production implementation
