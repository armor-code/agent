# DataDog Integration Guide

Sample DataDog integration for ArmorCode Agent metrics including reference shipper implementation, dashboard template, and sample data.

---

## 📦 Contents (Sample Files)

| File | Purpose |
|------|---------|
| `metrics_shipper.py` | **Sample** Python shipper service (reference implementation) |
| `metrics_shipper.env` | **Sample** configuration template (customize with your API keys, paths) |
| `metrics-shipper.service` | **Sample** systemd unit file (customize for your environment) |
| `datadog-dashboard.json` | **Sample** dashboard template (2 widgets - import and customize) |
| `sample-metrics.json` | Sample metrics data (549 real metrics for testing) |

**Note:** These are reference implementations and templates. Customize them for your specific environment and requirements.

---

## 🚀 Quick Setup

### 1. Install Dependencies
```bash
sudo pip3 install datadog
```

### 2. Configure Credentials
```bash
# Copy and edit configuration
sudo cp metrics_shipper.env /etc/armorcode/metrics_shipper.env
sudo vim /etc/armorcode/metrics_shipper.env

# Add your keys:
DATADOG_API_KEY=your_api_key_here
DATADOG_APP_KEY=your_app_key_here
```

Get keys from: https://app.datadoghq.com/organization-settings/

### 3. Install Service
```bash
# Copy files
sudo cp metrics_shipper.py /opt/armorcode/
sudo cp metrics-shipper.service /etc/systemd/system/

# Start service
sudo systemctl daemon-reload
sudo systemctl enable --now metrics-shipper

# Verify
sudo systemctl status metrics-shipper
curl http://localhost:9090/health | jq
```

### 4. Import Dashboard
**Via UI:**
1. Go to: https://app.datadoghq.com/dashboard/lists
2. Click "New Dashboard" → "Import Dashboard JSON"
3. Paste contents of `datadog-dashboard.json`

**Via API:**
```bash
curl -X POST "https://api.datadoghq.com/api/v1/dashboard" \
  -H "Content-Type: application/json" \
  -H "DD-API-KEY: ${DATADOG_API_KEY}" \
  -H "DD-APPLICATION-KEY: ${DATADOG_APP_KEY}" \
  -d @datadog-dashboard.json
```

### 5. Verify in DataDog
1. Go to: https://app.datadoghq.com/metric/explorer
2. Search: `armorcode.http.request.duration_ms`
3. Filter by: `operation:get_task`

---

## 🚢 Metrics Shipper

### How It Works

```
Worker → /tmp/armorcode/log/metrics/*.json → Shipper → DataDog
         ├─ Position tracking (no duplicates)
         ├─ File rotation detection (inode-based)
         ├─ Batch processing (100 metrics / 5 seconds)
         └─ Health check (:9090/health)
```

**Key Features:**
- **No Duplicates**: Tracks (file_path, inode, position) tuples
- **Auto Recovery**: Resumes from last position after restart
- **Zero Impact**: <5% CPU, <100 MB memory
- **Auto-start**: Systemd service with restart on failure

### Configuration

Edit `/etc/armorcode/metrics_shipper.env`:

| Variable | Default | Description |
|----------|---------|-------------|
| `DATADOG_API_KEY` | **(required)** | DataDog API key |
| `DATADOG_APP_KEY` | **(required)** | DataDog App key |
| `DATADOG_SITE` | `datadoghq.com` | Region (US/EU) |
| `METRICS_DIR` | `/tmp/armorcode/log/metrics` | Metrics folder |
| `METRICS_PATTERN` | `metrics*.json` | File pattern |
| `BATCH_SIZE` | `100` | Metrics per batch |
| `BATCH_TIMEOUT_SEC` | `5` | Max wait before flush |
| `HEALTH_CHECK_PORT` | `9090` | Health endpoint port |

### Monitoring

**Health Check:**
```bash
curl http://localhost:9090/health | jq
```

**Response:**
```json
{
  "status": "healthy",
  "uptime_seconds": 300,
  "files_monitored": 3,
  "metrics_shipped": 1234,
  "last_ship_time": "2024-10-28T12:05:00Z",
  "errors_last_hour": 0,
  "datadog_connected": true
}
```

**Logs:**
```bash
# Live tail
sudo journalctl -u metrics-shipper -f

# Errors only
sudo journalctl -u metrics-shipper | grep -i error
```

### Troubleshooting

**Service won't start:**
```bash
# Test directly
sudo -u armorcode python3 /opt/armorcode/metrics_shipper.py

# Check config
sudo cat /etc/armorcode/metrics_shipper.env | grep DATADOG
```

**Metrics not in DataDog:**
```bash
# Check service
sudo systemctl status metrics-shipper

# Check health
curl http://localhost:9090/health | jq -r '.status'

# Check recent shipments
sudo journalctl -u metrics-shipper --since "5 min ago" | grep shipped
```

**Slow delivery:**
```bash
# Reduce timeout
sudo vim /etc/armorcode/metrics_shipper.env
# Set: BATCH_TIMEOUT_SEC=2
sudo systemctl restart metrics-shipper
```

### Maintenance

**Restart:**
```bash
sudo systemctl restart metrics-shipper
```

**Update config:**
```bash
sudo vim /etc/armorcode/metrics_shipper.env
sudo systemctl restart metrics-shipper
```

**Upgrade:**
```bash
sudo systemctl stop metrics-shipper
sudo cp metrics_shipper.py /opt/armorcode/
sudo systemctl start metrics-shipper
```

---

## 📊 Dashboard

### Included Widgets

**1. Average Request Duration**
- Query: `avg:armorcode.http.request.duration_ms{*} by {operation}.rollup(avg, 300)`
- Shows: Average duration per operation (5-min window)
- Type: Line chart

**2. Total Request Count**
- Query: `sum:armorcode.http.request.duration_ms{*} by {operation}.as_count().rollup(sum, 300)`
- Shows: Total requests per operation (5-min window)
- Type: Bar chart

### Adding More Widgets

**P95 Latency:**
```
p95:armorcode.http.request.duration_ms{*} by {operation}
```

**Error Rate:**
```
sum:armorcode.http.request.duration_ms{success:false} by {operation}.as_count()
```

**Rate Limit Errors:**
```
sum:armorcode.http.request.duration_ms{status_code:429}.as_count()
```

### Query Syntax

**Important:** Order matters!

```
metric{filter} by {grouping}.function().rollup(agg, seconds)
```

**Examples:**
```
# Average by operation
avg:armorcode.http.request.duration_ms{*} by {operation}.rollup(avg, 300)

# Count by domain
sum:armorcode.http.request.duration_ms{*} by {domain}.as_count()

# Filter errors
sum:armorcode.http.request.duration_ms{success:false}
```

---

## 📈 Sample Data

### `sample-metrics.json`

**Stats:**
- 549 metrics from production worker
- All `get_task` operations (polling for tasks)
- Mix of 200 (task available) and 204 (no task)

**Test Publishing:**
```bash
export DATADOG_API_KEY="your_key"
export DATADOG_APP_KEY="your_app_key"
export METRICS_DIR="$(pwd)"
export METRICS_PATTERN="sample-metrics.json"
python3 metrics_shipper.py
```

**Use for:**
- Testing DataDog integration
- Validating dashboard queries
- Training team on metrics format

---

## 🎯 DataDog Filters

### By Operation
```
{operation:get_task}
{operation:target_request}
{operation:upload_result}
```

### By Status
```
{status_code:200}     # Success
{status_code:204}     # No content
{status_code:429}     # Rate limited
{status_code:5*}      # Server errors
```

### By Success/Failure
```
{success:true}        # Successful
{success:false}       # Failed
```

### By Domain
```
{domain:api.github.com}
{domain:app.armorcode.com}
```

---

## 🚨 Recommended Alerts

| Alert | Condition | Severity |
|-------|-----------|----------|
| Shipper down | Service inactive > 5 min | Critical |
| High error rate | `errors_last_hour > 10` | Warning |
| Not shipping | `last_ship_time` > 2 min old | Warning |
| DataDog disconnected | `datadog_connected == false` | Critical |
| High latency | `p95:armorcode.http.request.duration_ms > 5000` | Warning |
| Rate limiting | `sum:armorcode.http.request.duration_ms{status_code:429} > 5` | Warning |

---

## 🔒 Security

**File Permissions:**
```bash
# Config file (contains API keys!)
sudo chmod 600 /etc/armorcode/metrics_shipper.env

# Service runs as armorcode user (not root)
ps aux | grep metrics_shipper  # Should show: armorcode user
```

**Network:**
- Outbound HTTPS (443) to `api.datadoghq.com`
- TLS 1.2+ required

---

## 📚 Related Documentation

- **Complete metrics spec:** `../METRICS_README.md`
- **Package overview:** `../README.md`
- **DataDog Metrics Explorer:** https://app.datadoghq.com/metric/explorer
- **DataDog Query Docs:** https://docs.datadoghq.com/dashboards/querying/

---

**Version:** 1.0.0
**Last Updated:** 2024-10-28
**Support:** Check logs → Health endpoint → ../METRICS_README.md → ArmorCode Engineering
