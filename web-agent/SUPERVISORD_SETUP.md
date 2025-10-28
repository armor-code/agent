# ArmorCode Web Agent - Systemd Service Setup

## Overview

This guide provides instructions for setting up the ArmorCode Web Agent as a systemd service for robust process management, automatic restarts, and centralized logging.

## Prerequisites

- **Python 3.9 or higher** is required
- **Root/sudo access** for system service installation
- **Internet connectivity** to download required files

## Installation Steps

### 1. Create directories and download required files

```bash
sudo mkdir -p /opt/armorcode
sudo wget -O /opt/armorcode/worker.py 'https://raw.githubusercontent.com/armor-code/agent/refs/heads/main/web-agent/app/worker.py'
wget -O requirements.txt 'https://raw.githubusercontent.com/armor-code/agent/refs/heads/main/web-agent/requirements.txt'
pip3 install -r requirements.txt
```

### 2. Service Configuration

**Create a systemd service file using vi/nano:**
```bash
sudo vi /etc/systemd/system/armorcode-agent.service
```

**Copy and paste the following configuration:**

```ini
[Unit]
Description=Run Armorcode agent Python Script at Startup
After=network.target

[Service]
WorkingDirectory=/opt/armorcode
ExecStart=<PYTHON_PATH> worker.py --serverUrl=https://web-agent.armorcode.com --apiKey=<API_KEY>
Restart=always
RestartSec=5s
User=<USER>
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

**Configuration placeholders:**
- `<PYTHON_PATH>` - Replace with `/usr/bin/python3` (or your Python path)
- `<API_KEY>` - Replace with your ArmorCode API key
- `<USER>` - Replace with `root` or your preferred user

**Or download the sample service file:**
```bash
sudo wget -O /etc/systemd/system/armorcode-agent.service 'https://raw.githubusercontent.com/armor-code/agent/refs/heads/main/web-agent/armorcode-agent.service'
```

### 3. Service Management

**Enable and start the service:**
```bash
sudo systemctl daemon-reload
sudo systemctl enable armorcode-agent.service
sudo systemctl start armorcode-agent.service
```

**Check service status:**
```bash
sudo systemctl status armorcode-agent.service
```

**View service logs:**
```bash
sudo journalctl -u armorcode-agent.service -f
```

**Stop the service:**
```bash
sudo systemctl stop armorcode-agent.service
```

**Restart the service:**
```bash
sudo systemctl restart armorcode-agent.service
```

**Disable service (prevent auto-start):**
```bash
sudo systemctl disable armorcode-agent.service
```

## Configuration Options

Add these parameters to the `ExecStart` line as needed:

**Proxy Configuration:**
```bash
--outgoingProxyHttps='https://proxy.example.com:8080'
--inwardProxyHttps='https://internal-proxy.example.com:8080'
--inwardProxyHttp='http://internal-proxy.example.com:8080'
```

**Environment Name:**
```bash
--envName='production'
```

**Complete example with all options:**
```ini
ExecStart=/usr/bin/python3 worker.py --serverUrl=https://web-agent.armorcode.com --apiKey=your_api_key --envName=production --outgoingProxyHttps=https://proxy.example.com:8080
```

## Alternative: Using Environment Files

Instead of passing configuration as command-line arguments, you can use an environment file for better credential management and cleaner service definitions.

### Benefits of Environment Files:
- **Better Security**: Keep sensitive credentials out of service files
- **Easier Management**: Update configuration without modifying service files
- **Cleaner Service Definitions**: Separate configuration from service logic
- **Version Control**: Can exclude .env files from git while committing service files

### Setup Steps:

**1. Create environment file:**
```bash
sudo mkdir -p /etc/armorcode
sudo vi /etc/armorcode/armorcode-agent.env
```

**2. Add configuration to environment file:**
```bash
# ArmorCode Agent Configuration
# Required settings
SERVER_URL=https://web-agent.armorcode.com
API_KEY=your_api_key_here

# Optional settings
ENV_NAME=production
OUTGOING_PROXY_HTTPS=https://proxy.example.com:8080
INWARD_PROXY_HTTPS=https://internal-proxy.example.com:8080
INWARD_PROXY_HTTP=http://internal-proxy.example.com:8080

# Python settings
PYTHONUNBUFFERED=1
```

**3. Secure the environment file:**
```bash
sudo chmod 600 /etc/armorcode/armorcode-agent.env
sudo chown root:root /etc/armorcode/armorcode-agent.env
```

**4. Update service file to use environment file:**
```bash
sudo vi /etc/systemd/system/armorcode-agent.service
```

**Modified service configuration:**
```ini
[Unit]
Description=Run Armorcode agent Python Script at Startup
After=network.target

[Service]
WorkingDirectory=/opt/armorcode
ExecStart=<PYTHON_PATH> worker.py --serverUrl=${SERVER_URL} --apiKey=${API_KEY} --envName=${ENV_NAME} --outgoingProxyHttps=${OUTGOING_PROXY_HTTPS} --inwardProxyHttps=${INWARD_PROXY_HTTPS} --inwardProxyHttp=${INWARD_PROXY_HTTP}
EnvironmentFile=/etc/armorcode/armorcode-agent.env
Restart=always
RestartSec=5s
User=<USER>

[Install]
WantedBy=multi-user.target
```

**5. Reload and restart service:**
```bash
sudo systemctl daemon-reload
sudo systemctl restart armorcode-agent.service
sudo systemctl status armorcode-agent.service
```

### Environment File Variables:

| Variable | Required | Description | Example |
|----------|----------|-------------|---------|
| `SERVER_URL` | Yes | ArmorCode server URL | `https://web-agent.armorcode.com` |
| `API_KEY` | Yes | Your ArmorCode API key | `abc123...` |
| `ENV_NAME` | No | Environment name | `production`, `staging` |
| `OUTGOING_PROXY_HTTPS` | No | HTTPS proxy for ArmorCode API | `https://proxy.example.com:8080` |
| `INWARD_PROXY_HTTPS` | No | HTTPS proxy for internal tools | `https://internal-proxy.example.com:8080` |
| `INWARD_PROXY_HTTP` | No | HTTP proxy for internal tools | `http://internal-proxy.example.com:8080` |
| `PYTHONUNBUFFERED` | No | Python output buffering | `1` (recommended) |

### Example: Minimal Configuration

For a simple setup with just required parameters:

**Environment file** (`/etc/armorcode/armorcode-agent.env`):
```bash
SERVER_URL=https://web-agent.armorcode.com
API_KEY=your_api_key_here
PYTHONUNBUFFERED=1
```

**Service file** (`/etc/systemd/system/armorcode-agent.service`):
```ini
[Unit]
Description=Run Armorcode agent Python Script at Startup
After=network.target

[Service]
WorkingDirectory=/opt/armorcode
ExecStart=/usr/bin/python3 worker.py --serverUrl=${SERVER_URL} --apiKey=${API_KEY}
EnvironmentFile=/etc/armorcode/armorcode-agent.env
Restart=always
RestartSec=5s
User=root

[Install]
WantedBy=multi-user.target
```

**Note**: This pattern follows the same approach used by the metrics-shipper service (see `app/Metrics/DataDog/metrics-shipper.service` for reference).

## Troubleshooting

### Service fails to start
```bash
# Check service status
sudo systemctl status armorcode-agent.service

# Check detailed logs
sudo journalctl -u armorcode-agent.service -n 50
```

### Python version check
```bash
python3 --version  # Should be 3.9 or higher
```

### Network connectivity test
```bash
curl -I https://web-agent.armorcode.com
```

### File permissions
```bash
ls -la /opt/armorcode/
# worker.py should be readable by the service user
```
