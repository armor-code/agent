# Worker Metrics Documentation

## Overview

The worker application automatically collects performance and operational metrics to help monitor agent health, identify bottlenecks, and analyze system behavior. All metrics are logged to JSON files with automatic daily rotation.

---

## Complete Tag Reference

This section provides a unified reference for ALL tags used across all metrics. Use this as a quick lookup guide.

### Core Tags (Used Across Multiple Metrics)

| Tag Name | Type | Description | Used By | Possible Values | Always Present |
|----------|------|-------------|---------|-----------------|----------------|
| `task_id` | string | Unique task identifier from ArmorCode | All metrics | `"1234567890_abc123"`, `"none"` | Yes |
| `operation` | string | Type of HTTP operation | `http.request.duration_ms` | `"get_task"`, `"upload_result"`, `"upload_file"`, `"target_request"` | Yes |
| `url` | string | Request URL (without query params) | `http.request.duration_ms` | `"https://app.armorcode.com/api/..."` | Yes |
| `domain` | string | Domain of the request | `http.request.duration_ms`, `task.processing_duration_ms` | `"app.armorcode.com"`, `"api.github.com"`, `"unknown"` | Yes |
| `method` | string | HTTP method | `http.request.duration_ms`, `task.processing_duration_ms` | `"GET"`, `"POST"`, `"PUT"`, `"DELETE"` | Yes |
| `status_code` | string | HTTP status code | `http.request.duration_ms` | `"200"`, `"204"`, `"429"`, `"500"`, `"unknown"` | Yes |

### Operation-Specific Tags

| Tag Name | Type | Description | Used By | Possible Values | When Present |
|----------|------|-------------|---------|-----------------|--------------|
| `success` | string | Whether request succeeded | `http.request.duration_ms` (all ops) | `"true"` (< 400), `"false"` (≥ 400) | Always for target_request |
| `has_task` | string | Whether task was returned | `http.request.duration_ms` (get_task only) | `"true"`, `"false"` | Only for get_task |
| `error_type` | string | Error classification | `http.request.duration_ms` (upload_result only) | `"rate_limit"`, `"timeout"`, `"server_error"`, `"client_error"` | Only on upload failures |
| `http_status` | string | HTTP status from target | `task.processing_duration_ms` | `"200"`, `"404"`, `"500"`, `"unknown"` | Always |
| `upload_type` | string | Upload method used | `upload.size_bytes` | `"inline"` (≤ 512 bytes), `"direct"` (> 512 bytes) | Always |

### Tag Value Rules

**task_id**:
- Normal: `"{timestamp}_{random_string}"` (e.g., `"1704067200_abc123"`)
- No task available: `"none"`

**domain**:
- Valid URL: Extracted domain (e.g., `"api.github.com"`)
- Invalid URL: `"unknown"`
- Missing URL: `"unknown"`

**status_code**:
- HTTP response: Actual code (e.g., `"200"`, `"429"`)
- Network error: `"unknown"`
- Timeout: `"unknown"`

**http_status** (task.processing_duration_ms):
- Same as status_code but for the target request only

**success**:
- Status < 400: `"true"`
- Status ≥ 400: `"false"`

**has_task** (get_task only):
- Status 200: `"true"` (task received)
- Status 204: `"false"` (no task available)

**error_type** (upload_result failures):
- Status 429: `"rate_limit"`
- Status 408/504: `"timeout"`
- Status 500-599: `"server_error"`
- Status 400-499 (except 408, 429): `"client_error"`

**upload_type**:
- File size ≤ 512 bytes: `"inline"`
- File size > 512 bytes: `"direct"`

---

## Metrics File Location

**Path**: `/tmp/armorcode/log/metrics/metrics{agent_index}.json`

- **Default**: `/tmp/armorcode/log/metrics/metrics_prod.json`
- **Example**: `/tmp/armorcode/log/metrics/metrics_1.json`

**Rotation**:
- Files rotate daily at midnight
- Retention: Configurable (default 7 days)
- Format: `metrics{agent_index}.json.YYYY-MM-DD`
- Configure: `--metricsRetentionDays N` or env var `metricsRetentionDays`

---

## Metrics Collected

### 1. HTTP Request Duration (`http.request.duration_ms`)

Measures the duration of HTTP requests made by the worker.

#### Operations Tracked

| Operation | Description | When It's Logged |
|-----------|-------------|------------------|
| `get_task` | Fetching tasks from ArmorCode server | Every task poll request |
| `upload_result` | Uploading task results to server | When updating task status |
| `upload_file` | Uploading large files to server | When file size > 512 bytes |
| `target_request` | HTTP request to target URL | Every task execution |

#### Tags

| Tag | Type | Description | Example Values |
|-----|------|-------------|----------------|
| `task_id` | string | Unique task identifier | `"1234567890_abc123"`, `"none"` |
| `operation` | string | Type of operation | `"get_task"`, `"upload_result"`, `"upload_file"`, `"target_request"` |
| `url` | string | Request URL (without query params) | `"https://app.armorcode.com/api/http-teleport/get-task"` |
| `domain` | string | Domain of the request | `"app.armorcode.com"`, `"api.github.com"` |
| `method` | string | HTTP method | `"GET"`, `"POST"`, `"PUT"`, `"DELETE"` |
| `status_code` | string | HTTP status code | `"200"`, `"204"`, `"429"`, `"500"` |
| `success` | string | Whether request succeeded | `"true"` (status < 400), `"false"` (status ≥ 400) |
| `has_task` | string | (get_task only) Whether task was returned | `"true"`, `"false"` |
| `error_type` | string | (upload_result only) Error classification | `"rate_limit"`, `"timeout"`, `"server_error"`, `"client_error"` |

#### Example Metrics

**Successful get-task with task:**
```json
{
  "@timestamp": 1704067200000,
  "metric_name": "http.request.duration_ms",
  "value": 234.56,
  "tags": {
    "task_id": "1704067200_abc123",
    "operation": "get_task",
    "url": "https://app.armorcode.com/api/http-teleport/get-task",
    "domain": "app.armorcode.com",
    "method": "GET",
    "status_code": "200",
    "has_task": "true"
  }
}
```

**Get-task with no task available:**
```json
{
  "@timestamp": 1704067205000,
  "metric_name": "http.request.duration_ms",
  "value": 187.23,
  "tags": {
    "task_id": "none",
    "operation": "get_task",
    "url": "https://app.armorcode.com/api/http-teleport/get-task",
    "domain": "app.armorcode.com",
    "method": "GET",
    "status_code": "204",
    "has_task": "false"
  }
}
```

**Target request (actual task execution):**
```json
{
  "@timestamp": 1704067210000,
  "metric_name": "http.request.duration_ms",
  "value": 1523.45,
  "tags": {
    "task_id": "1704067200_abc123",
    "operation": "target_request",
    "url": "https://api.github.com/repos/owner/repo/issues",
    "domain": "api.github.com",
    "method": "GET",
    "status_code": "200",
    "success": "true"
  }
}
```

**Upload result with rate limit error:**
```json
{
  "@timestamp": 1704067215000,
  "metric_name": "http.request.duration_ms",
  "value": 145.67,
  "tags": {
    "task_id": "1704067200_abc123",
    "operation": "upload_result",
    "url": "https://app.armorcode.com/api/http-teleport/put-result",
    "domain": "app.armorcode.com",
    "method": "POST",
    "status_code": "429",
    "success": "false",
    "error_type": "rate_limit"
  }
}
```

---

### 2. Task Processing Duration (`task.processing_duration_ms`)

Measures the total time to process a task from start to finish.

#### Tags

| Tag | Type | Description | Example Values |
|-----|------|-------------|----------------|
| `task_id` | string | Unique task identifier | `"1704067200_abc123"` |
| `method` | string | HTTP method of target request | `"GET"`, `"POST"` |
| `domain` | string | Domain of target URL | `"api.github.com"` |
| `http_status` | string | HTTP status from target | `"200"`, `"404"`, `"500"`, `"unknown"` |

#### Example Metric

```json
{
  "@timestamp": 1704067220000,
  "metric_name": "task.processing_duration_ms",
  "value": 2345.89,
  "tags": {
    "task_id": "1704067200_abc123",
    "method": "GET",
    "domain": "api.github.com",
    "http_status": "200"
  }
}
```

---

### 3. Upload Size (`upload.size_bytes`)

Tracks the size of data uploaded to ArmorCode server.

#### Upload Types

| Upload Type | Description | When It Happens |
|-------------|-------------|-----------------|
| `inline` | Small responses sent in JSON | File size ≤ 512 bytes |
| `direct` | Large files uploaded separately | File size > 512 bytes |

#### Tags

| Tag | Type | Description | Example Values |
|-----|------|-------------|----------------|
| `task_id` | string | Unique task identifier | `"1704067200_abc123"` |
| `upload_type` | string | How data was uploaded | `"inline"`, `"direct"` |

#### Example Metrics

**Inline upload:**
```json
{
  "@timestamp": 1704067225000,
  "metric_name": "upload.size_bytes",
  "value": 342,
  "tags": {
    "task_id": "1704067200_abc123",
    "upload_type": "inline"
  }
}
```

**Direct file upload:**
```json
{
  "@timestamp": 1704067230000,
  "metric_name": "upload.size_bytes",
  "value": 1548672,
  "tags": {
    "task_id": "1704067200_abc123",
    "upload_type": "direct"
  }
}
```

---

## Metric Flow for a Single Task

Here's the complete metric timeline for processing one task:

```
1. Get Task Request
   ├─ Metric: http.request.duration_ms
   ├─ Operation: get_task
   └─ Result: Received task with ID "1704067200_abc123"

2. Target Request (Execute Task)
   ├─ Metric: http.request.duration_ms
   ├─ Operation: target_request
   └─ Result: Fetched data from target URL

3. Upload Response (if file > 512 bytes)
   ├─ Metric: http.request.duration_ms
   ├─ Operation: upload_file
   └─ Metric: upload.size_bytes (direct)
   OR
   └─ Metric: upload.size_bytes (inline, if ≤ 512 bytes)

4. Update Task Status
   ├─ Metric: http.request.duration_ms
   └─ Operation: upload_result

5. Task Processing Complete
   ├─ Metric: task.processing_duration_ms
   └─ Result: Total time from step 2 to step 4
```

---

## Configuration

### Buffer Settings

Metrics are buffered in memory before being written to disk:

| Setting | Default | Description |
|---------|---------|-------------|
| `flush_interval` | 10 seconds | How often to flush buffer to disk |
| `buffer_size` | 1000 metrics | Max metrics before auto-flush |

### File Rotation

Metrics files rotate daily at midnight with configurable retention:

| Setting | Default | Description | Configuration |
|---------|---------|-------------|---------------|
| `retention_days` | 7 days | Number of days to retain old metrics files | Command line or environment variable |

**Configure via command line:**
```bash
# Keep metrics for 14 days
python worker.py --metricsRetentionDays 14

# Keep metrics for 30 days
python worker.py --metricsRetentionDays 30
```

**Configure via environment variable:**
```bash
# Set retention to 21 days
export metricsRetentionDays=21
python worker.py
```

**File naming pattern:**
- Current: `metrics{agent_index}.json`
- Rotated: `metrics{agent_index}.json.YYYY-MM-DD`
- Example: `metrics_prod.json.2024-01-15`

**Automatic cleanup:** Files older than the retention period are automatically deleted.

### Shutdown Behavior

When the worker stops (SIGTERM, SIGINT, or normal exit):
- All buffered metrics are immediately flushed to disk
- The flush thread is gracefully stopped
- No metrics are lost

---

## Error Handling & Resilience

### Automatic Error Recovery

All metrics logging is **completely isolated** from worker functionality:

- **URL parsing errors** → Logs `"unknown"` for domain/URL
- **Metrics write errors** → Logged at DEBUG level, worker continues
- **Disk full** → Worker continues operating normally
- **Invalid data** → Defaults to safe values (`"unknown"`, `"none"`)

### Debug Logging

Failed metrics operations are logged to the main application log:

```
2024-01-01 12:00:00 - worker - DEBUG - worker.py:747 - Metrics logging failed: [error details]
```

---

## Analysis & Monitoring

### Common Queries

#### 1. Average Request Duration by Operation

```bash
# Using jq to analyze metrics file
cat /tmp/armorcode/metrics/metrics_prod.json | \
jq -r 'select(.metric_name == "http.request.duration_ms") |
       "\(.tags.operation),\(.value)"' | \
awk -F',' '{sum[$1]+=$2; count[$1]++}
            END {for (op in sum) print op": "sum[op]/count[op]" ms"}'
```

**Example Output:**
```
get_task: 234.56 ms
upload_result: 145.23 ms
target_request: 1234.89 ms
upload_file: 567.34 ms
```

#### 2. Count Requests by Status Code

```bash
cat /tmp/armorcode/metrics/metrics_prod.json | \
jq -r 'select(.metric_name == "http.request.duration_ms") |
       .tags.status_code' | \
sort | uniq -c
```

**Example Output:**
```
   1234 200
     45 204
      5 429
      2 500
```

#### 3. Find Slow Tasks (> 5 seconds)

```bash
cat /tmp/armorcode/metrics/metrics_prod.json | \
jq 'select(.metric_name == "task.processing_duration_ms" and .value > 5000) |
    {task_id: .tags.task_id, duration_sec: (.value/1000), domain: .tags.domain}'
```

**Example Output:**
```json
{"task_id":"1704067200_abc123","duration_sec":7.234,"domain":"slow-api.example.com"}
{"task_id":"1704067300_def456","duration_sec":12.567,"domain":"timeout-api.example.com"}
```

#### 4. Total Data Uploaded

```bash
cat /tmp/armorcode/metrics/metrics_prod.json | \
jq -r 'select(.metric_name == "upload.size_bytes") | .value' | \
awk '{sum+=$1; count++} END {print "Total: "sum/1024/1024" MB in "count" uploads"}'
```

**Example Output:**
```
Total: 2345.67 MB in 1234 uploads
```

#### 5. Error Rate by Operation

```bash
cat /tmp/armorcode/metrics/metrics_prod.json | \
jq -r 'select(.metric_name == "http.request.duration_ms") |
       "\(.tags.operation),\(.tags.success)"' | \
awk -F',' '{total[$1]++; if($2=="false") errors[$1]++}
            END {for (op in total) print op": "((errors[op]/total[op])*100)"% error rate"}'
```

**Example Output:**
```
get_task: 0.5% error rate
upload_result: 2.3% error rate
target_request: 5.1% error rate
```

---

## Integration with Monitoring Systems

### DataDog

The JSON format is compatible with DataDog's JSON log ingestion:

```json
{
  "@timestamp": 1704067200000,
  "metric_name": "http.request.duration_ms",
  "value": 234.56,
  "tags": {...}
}
```

**DataDog Agent Configuration:**
```yaml
logs:
  - type: file
    path: /tmp/armorcode/metrics/metrics*.json
    service: armorcode-agent
    source: custom
    sourcecategory: metrics
```

### Prometheus

Convert to Prometheus format using a sidecar exporter or custom script:

```python
# Example: Convert to Prometheus exposition format
# http_request_duration_ms{operation="get_task",status_code="200"} 234.56
```

### Elasticsearch

Bulk import using Logstash or Filebeat:

```json
{
  "input": {
    "type": "log",
    "paths": ["/tmp/armorcode/metrics/metrics*.json"]
  },
  "output": {
    "elasticsearch": {
      "hosts": ["localhost:9200"],
      "index": "armorcode-metrics-%{+YYYY.MM.dd}"
    }
  }
}
```

---

## Troubleshooting

### Metrics File Not Created

**Check:**
1. Folder exists: `ls -la /tmp/armorcode/metrics/`
2. Permissions: `ls -l /tmp/armorcode/metrics/metrics_prod.json`
3. Worker logs: Look for initialization errors

**Solution:**
```bash
# Manually create folder with correct permissions
mkdir -p /tmp/armorcode/metrics
chmod 755 /tmp/armorcode/metrics
```

### Metrics Not Being Written

**Check:**
1. Disk space: `df -h /tmp`
2. Worker debug logs: Enable `--debugMode true`
3. Buffer status: Metrics flush every 10 seconds or 1000 entries

**Solution:**
```bash
# Send SIGTERM to flush remaining metrics
kill -TERM <worker_pid>
```

### Invalid JSON Format

**Check:**
- Are multiple workers writing to same file?
- Was the file manually edited?

**Solution:**
```bash
# Each worker should have unique agent index
python worker.py --index _1  # writes to metrics_1.json
python worker.py --index _2  # writes to metrics_2.json
```

---

## Best Practices

### 1. Unique Agent Indices
When running multiple workers, use unique indices:
```bash
python worker.py --index _prod  # Production
python worker.py --index _test  # Testing
python worker.py --index _1     # Worker 1
python worker.py --index _2     # Worker 2
```

### 2. Regular Analysis
Schedule daily analysis of metrics:
```bash
# Cron job example: Daily summary at midnight
0 0 * * * /path/to/analyze_metrics.sh
```

### 3. Alert on Anomalies
Set up alerts for:
- Error rate > 5%
- Average task duration > 10 seconds
- 5XX errors from ArmorCode server
- Rate limit errors (429) increasing

### 4. Archive Old Metrics
Metrics are auto-rotated (7 days), but consider archiving:
```bash
# Archive metrics older than 7 days to S3
find /tmp/armorcode/metrics -name "metrics*.json.*" -mtime +7 -exec \
  aws s3 cp {} s3://backup-bucket/metrics/ \;
```

### 5. Monitor Disk Space
Metrics files can grow large with high task volume:
```bash
# Estimate: ~500 bytes per task × 1000 tasks/day = ~500KB/day
# 7 days retention = ~3.5MB (minimal)
```

---

## Performance Impact

### Memory Usage
- **Buffer**: ~1000 metrics × 500 bytes = ~500KB
- **Thread**: Minimal (sleep-based flush loop)

### CPU Usage
- **JSON serialization**: < 1% CPU
- **File I/O**: Async writes every 10 seconds

### Disk I/O
- **Writes**: Batch writes every 10 seconds
- **Size**: ~500 bytes per metric event

**Conclusion**: Metrics collection has **negligible performance impact** on worker operations.

---

## Version History

| Version | Date | Changes |
|---------|------|---------|
| 1.0.0 | 2024-01-01 | Initial metrics implementation |
| | | - HTTP request duration tracking |
| | | - Task processing duration tracking |
| | | - Upload size tracking |
| | | - Error-resilient design |
| | | - Auto-rotation (7 days) |

---

## Support

For questions or issues with metrics:
1. Check worker debug logs: `--debugMode true`
2. Verify metrics file: `cat /tmp/armorcode/metrics/metrics_prod.json | jq`
3. Review this documentation
4. Contact ArmorCode support

---

**Last Updated**: 2024-01-01
**Maintained By**: ArmorCode Engineering Team
