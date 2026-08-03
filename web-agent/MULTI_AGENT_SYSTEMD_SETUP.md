# ArmorCode Web Agent - Multiple Agents via a Single Systemd Service

## Overview

This guide explains how to run **multiple ArmorCode Web Agents on one host** as a
**single, fate-shared systemd service**.

A small supervisor script (`run-agents.sh`) launches every agent and watches them
together. The behaviour is intentionally **coupled**:

- If **any one** agent process stops for any reason, the supervisor terminates the
  remaining agents and exits.
- Because the systemd unit is configured with `Restart=always`, systemd then restarts
  the whole group together.

This is useful when you need several agents (for example, one per environment/index)
managed as a single unit with a shared lifecycle, rather than as independent services.

> If instead you want each agent to start/stop/restart **independently**, use one
> service per agent (see [SUPERVISORD_SETUP.md](SUPERVISORD_SETUP.md)) or a systemd
> template unit. This guide is specifically for the coupled, all-together model.

## Prerequisites

- **Python 3.9 or higher** is required.
- **Bash 5.1 or higher** is required (the supervisor uses the scoped `wait -n <pids>`
  form). Check with `bash --version`.
- **Root/sudo access** for system service installation.
- **Internet connectivity** to download required files.

## Installation Steps

### 1. Create directories and download required files

```bash
sudo mkdir -p /opt/armorcode
sudo wget -O /opt/armorcode/worker.py 'https://raw.githubusercontent.com/armor-code/agent/refs/heads/main/web-agent/app/worker.py'
wget -O requirements.txt 'https://raw.githubusercontent.com/armor-code/agent/refs/heads/main/web-agent/requirements.txt'
pip3 install -r requirements.txt
```

### 2. Create the supervisor script

**Create the script using vi/nano:**
```bash
sudo vi /opt/armorcode/run-agents.sh
```

**Copy and paste the following:**

```bash
#!/usr/bin/env bash
#
# run-agents.sh — Launch and supervise multiple ArmorCode agents as ONE unit.
# If ANY agent exits, all remaining agents are terminated and the script exits
# non-zero, so systemd (Restart=always) restarts the whole group together.
#
# Invoked by systemd (no arguments). Edit WORKER_SCRIPT / AGENTS below.
#
# Contains live API keys — keep mode 600 and out of version control.
# Requires bash >= 5.1 (for the scoped `wait -n <pids>` form).

set -euo pipefail

# --- Configuration -----------------------------------------------------------
# Hardcoded because this is launched by systemd, not interactively.
WORKER_SCRIPT="/opt/armorcode/worker.py"
WORKDIR="$(dirname "$WORKER_SCRIPT")"      # run from the worker's own directory
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3)}"   # override via env if needed
SERVER_URL="https://web-agent.armorcode.com"
STOP_GRACE_SECONDS=10

# One entry per agent = the args passed to worker.py.
# Add or remove lines to match the number of agents you need.
AGENTS=(
  "--serverUrl ${SERVER_URL} --apiKey REPLACE_WITH_KEY_1 --index _agent1"
  "--serverUrl ${SERVER_URL} --apiKey REPLACE_WITH_KEY_2 --index _agent2"
)

# --- Runtime -----------------------------------------------------------------
declare -a PIDS=()

log() { printf '%s [run-agents] %s\n' "$(date -u +%FT%TZ)" "$*"; }

# Mask the API key value before logging the command line.
redact() { sed -E 's/(--apiKey )[^ ]+/\1***/g' <<<"$1"; }

terminate_all() {
  trap - TERM INT EXIT          # disable traps so cleanup is not re-entered
  log "Terminating ${#PIDS[@]} agent(s)…"
  for pid in "${PIDS[@]}"; do kill -TERM "$pid" 2>/dev/null || true; done

  local deadline=$((SECONDS + STOP_GRACE_SECONDS))
  for pid in "${PIDS[@]}"; do
    while kill -0 "$pid" 2>/dev/null && (( SECONDS < deadline )); do sleep 0.2; done
    kill -KILL "$pid" 2>/dev/null || true   # force-kill stragglers
  done
}

# Clean up children on our own exit and on signals from systemd.
trap terminate_all TERM INT EXIT

cd "$WORKDIR"

for args in "${AGENTS[@]}"; do
  # Intentional word-splitting of $args into separate argv entries.
  # shellcheck disable=SC2086
  "$PYTHON_BIN" -u "$WORKER_SCRIPT" $args &
  pid=$!
  PIDS+=("$pid")
  log "Started pid=${pid}: $(basename "$WORKER_SCRIPT") $(redact "$args")"
done

log "Supervising ${#PIDS[@]} agent(s); any single exit brings the group down."

# Block until the FIRST of OUR agents exits, then capture its exit status.
# PID list scopes the wait to our agents only (ignores any other bg job).
# Requires bash >= 5.1 for the `wait -n <pids>` form.
if wait -n "${PIDS[@]}"; then exit_code=0; else exit_code=$?; fi

# Identify which agent(s) died (best-effort, for observability).
for pid in "${PIDS[@]}"; do
  kill -0 "$pid" 2>/dev/null || log "Agent pid=${pid} is no longer running."
done

log "An agent exited (status=${exit_code}); shutting down the group for restart."
exit "$exit_code"   # EXIT trap runs terminate_all; non-zero => systemd restarts
```

**Configuration placeholders:**
- `WORKER_SCRIPT` - Absolute path to `worker.py` (e.g. `/opt/armorcode/worker.py`).
- `SERVER_URL` - Your ArmorCode server URL.
- `AGENTS` - One entry per agent. Replace each `REPLACE_WITH_KEY_*` with a real API key
  and set a distinct `--index` per agent. Add/remove lines to match your agent count.

**Make it executable and secure it (the file holds API keys):**
```bash
sudo chmod 700 /opt/armorcode/run-agents.sh
sudo chown root:root /opt/armorcode/run-agents.sh
```

### 3. Service Configuration

**Create a systemd service file using vi/nano:**
```bash
sudo vi /etc/systemd/system/armorcode-agents.service
```

**Copy and paste the following configuration:**

```ini
[Unit]
Description=ArmorCode Agents (fate-shared supervised group)
Wants=network-online.target
After=network-online.target
# Crash-loop guard (systemd >= 230 expects these in [Unit]): give up if it
# fails >5 times within 60s.
StartLimitIntervalSec=60
StartLimitBurst=5

[Service]
Type=simple
# Absolute path to run-agents.sh on the deployment host.
ExecStart=/opt/armorcode/run-agents.sh
Environment=PYTHONUNBUFFERED=1
User=root

# One agent dying makes run-agents.sh exit; this restarts the whole group.
Restart=always
RestartSec=5

# Clean shutdown: SIGTERM the script (it forwards to children),
# then SIGKILL anything left in the cgroup after the timeout.
KillMode=mixed
KillSignal=SIGTERM
TimeoutStopSec=20

[Install]
WantedBy=multi-user.target
```

**Configuration placeholders:**
- `ExecStart` - Absolute path to `run-agents.sh`.
- `User` - Replace with `root` or your preferred user.

### 4. Service Management

**Enable and start the service:**
```bash
sudo systemctl daemon-reload
sudo systemctl enable armorcode-agents.service
sudo systemctl start armorcode-agents.service
```

**Check service status:**
```bash
sudo systemctl status armorcode-agents.service
```

**View service logs:**
```bash
sudo journalctl -u armorcode-agents.service -f
```

**Stop the service:**
```bash
sudo systemctl stop armorcode-agents.service
```

**Restart the service:**
```bash
sudo systemctl restart armorcode-agents.service
```

**Disable service (prevent auto-start):**
```bash
sudo systemctl disable armorcode-agents.service
```

## How the Fate-Sharing Works

1. `run-agents.sh` launches every entry in the `AGENTS` array as a background process
   and records their PIDs.
2. It then blocks on `wait -n "${PIDS[@]}"`, which returns the moment the **first** of
   those specific agents exits (the PID list ensures only our agents can trigger it).
3. On that event, the script's `EXIT`/signal trap runs `terminate_all`, which sends
   `SIGTERM` to the remaining agents and then `SIGKILL` to any stragglers after the
   grace period.
4. The script exits, so the systemd unit goes down. With `Restart=always`, systemd
   restarts the whole group after `RestartSec`.

**Crash-loop protection:** `StartLimitBurst=5` / `StartLimitIntervalSec=60` means that
if the service fails more than 5 times within 60 seconds, systemd stops retrying and
leaves the unit in a `failed` state until you run
`sudo systemctl reset-failed armorcode-agents.service` and start it again.

## Configuration Options

Each entry in the `AGENTS` array is a full set of arguments passed to `worker.py`. Add
optional flags per agent as needed:

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

**Example agent entry with options:**
```bash
"--serverUrl ${SERVER_URL} --apiKey your_api_key --index _agent1 --envName production --outgoingProxyHttps https://proxy.example.com:8080"
```

## Troubleshooting

### Service fails to start
```bash
sudo systemctl status armorcode-agents.service
sudo journalctl -u armorcode-agents.service -n 50
```

### Bash version check (must be >= 5.1)
```bash
bash --version
```
If the host has bash < 5.1, the scoped `wait -n "${PIDS[@]}"` form is unavailable and
the supervisor will not work as documented.

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
# run-agents.sh should be mode 700 and owned by the service user.
# worker.py should be readable by the service user.
```
