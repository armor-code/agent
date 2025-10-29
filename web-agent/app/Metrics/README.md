# ArmorCode Agent Metrics

Complete metrics collection system for monitoring agent performance and operational health.

---

## 📂 Contents

### `METRICS_README.md`
**Complete metrics specification** - Consumer-agnostic documentation covering:
- All metric types (HTTP requests, task processing, upload size)
- Complete tag reference (all tags in one place)
- JSON format examples
- Analysis queries (jq commands)
- Integration guides (DataDog, Prometheus, Elasticsearch)

### `DataDog/`
**DataDog integration folder** - Sample implementation and templates for DataDog:
- Sample metrics shipper service (Python reference implementation)
- Configuration template (.env file)
- Sample systemd service file
- Sample dashboard template (2 pre-configured widgets)
- Sample metrics data (549 real metrics)
- Complete setup guide

---

## 🚀 Quick Start

### 1. Understand the Metrics
Read: **`METRICS_README.md`**

The worker collects 3 metric types:
- `http.request.duration_ms` - All HTTP operations (get_task, upload_result, target_request)
- `task.processing_duration_ms` - Total task processing time
- `upload.size_bytes` - Data uploaded to ArmorCode

**Location:** `/tmp/armorcode/log/metrics/metrics{agent_index}.json`

### 2. Verify Collection
```bash
# Check metrics file
tail -f /tmp/armorcode/log/metrics/metrics_prod.json | jq

# Analyze locally
cat /tmp/armorcode/log/metrics/metrics_prod.json | \
  jq -r 'select(.metric_name == "http.request.duration_ms") |
         "\(.tags.operation): \(.value)ms"'
```

### 3. Set Up DataDog (Optional)
See: **`DataDog/README.md`**

```bash
cd DataDog/
# Follow setup guide in README.md
```

---

## 📊 Metric Types

| Metric | Tags | Description |
|--------|------|-------------|
| `http.request.duration_ms` | operation, domain, method, status_code, success | All HTTP requests |
| `task.processing_duration_ms` | task_id, domain, method, http_status | Complete task processing time |
| `upload.size_bytes` | task_id, upload_type | Data uploaded to ArmorCode |

**Complete specs:** See `METRICS_README.md`

---

## 🔧 Configuration

Metrics are automatically collected by `worker.py`:
- **Buffer size:** 1000 metrics
- **Flush interval:** 10 seconds
- **File location:** `/tmp/armorcode/log/metrics/`
- **Rotation:** Daily at midnight, 7-day retention

---

## 📁 Folder Structure

```
Metrics/
├── README.md                    # This file - entry point
├── METRICS_README.md            # Complete metrics specification
└── DataDog/                     # Sample DataDog integration
    ├── README.md               # DataDog setup guide
    ├── metrics_shipper.py      # Sample shipper service (reference implementation)
    ├── metrics_shipper.env     # Sample configuration template
    ├── metrics-shipper.service # Sample systemd unit file
    ├── datadog-dashboard.json  # Sample dashboard template (2 widgets)
    └── sample-metrics.json     # Sample metrics data (549 real metrics)
```

---

## 🎯 Use Cases

**Local analysis (no monitoring system):**
```bash
# See METRICS_README.md for complete analysis queries
cat /tmp/armorcode/log/metrics/metrics_prod.json | \
  jq -r 'select(.metric_name == "http.request.duration_ms") | ...'
```

**DataDog integration:**
```bash
# See DataDog/README.md for complete setup
cd DataDog/
# Setup shipper, import dashboard
```

**Other monitoring systems:**
- Metrics are in standard JSON format
- Easy integration with Prometheus, Elasticsearch, etc.
- See METRICS_README.md → "Integration with Monitoring Systems"

---

## 📚 Documentation

| Document | Purpose |
|----------|---------|
| **METRICS_README.md** | Complete metrics specification (start here) |
| **DataDog/README.md** | DataDog setup and shipper configuration |

---

**Version:** 1.0.0
**Last Updated:** 2024-10-28
**Maintained By:** ArmorCode Engineering Team
