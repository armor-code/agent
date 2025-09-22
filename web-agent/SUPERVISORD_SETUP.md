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

**Vi editor instructions:**
- Press `i` to enter insert mode
- Copy and paste the configuration below
- Press `Esc` to exit insert mode
- Type `:wq` and press `Enter` to save and quit

**Or use nano (easier for beginners):**
```bash
sudo nano /etc/systemd/system/armorcode-agent.service
```

**Nano editor instructions:**
- Copy and paste the configuration below
- Press `Ctrl+X` to exit
- Press `Y` to confirm save
- Press `Enter` to confirm filename

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

## Benefits of Systemd Setup

- **Automatic Restart:** Service automatically restarts if it crashes
- **Boot Integration:** Starts automatically when system boots
- **Centralized Logging:** All logs available via `journalctl`
- **Resource Management:** Better control over CPU/memory usage
- **Security:** Can run under dedicated user account with limited privileges
- **Process Monitoring:** Easy monitoring of service status and health