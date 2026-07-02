# ArmorCode Web Agent — Advanced Configuration Reference

This document covers all available CLI flags for `worker.py` / the Docker image.
The [main README](README.md) covers the essential setup flags. This document covers
the remaining flags for advanced tuning, troubleshooting, and production hardening.

---

## Complete Flag Reference

| Flag | Default | Description |
|------|---------|-------------|
| `--serverUrl` | — | ArmorCode server URL (required) |
| `--apiKey` | — | API key generated from the ArmorCode platform (required) |
| `--envName` | `""` | Environment name, if the agent is scoped to a specific env |
| `--outgoingProxyHttps` | `None` | HTTPS proxy for outbound calls to ArmorCode |
| `--outgoingProxyHttp` | `None` | HTTP proxy for outbound calls to ArmorCode |
| `--inwardProxyHttps` | `None` | HTTPS proxy for calls to internal tools (e.g. JIRA) |
| `--inwardProxyHttp` | `None` | HTTP proxy for calls to internal tools |
| `--verify` | `False` | Verify SSL certificates (`true`/`false`) |
| `--debugMode` | `False` | Enable DEBUG-level logging |
| `--enableStdoutLogging` | `False` | Print logs to stdout in addition to log files |
| `--index` | `_prod` | Agent instance identifier appended to log file names (useful when running multiple agents) |
| `--poolSize` | `5` | Number of concurrent greenlets for processing tasks |
| `--rateLimitPerMin` | `250` | Max requests per minute to the ArmorCode API |
| `--timeout` | `30` | General request timeout in seconds |
| `--connectTimeout` | `15` | TCP connect timeout in seconds for calls to internal tools |
| `--readTimeoutSeconds` | `100` | Read timeout in seconds for ArmorCode server calls |
| `--metricsRetentionDays` | `7` | Number of days to retain metrics log files |
| `--uploadToAc` | `True` | Upload large responses directly to ArmorCode (`true`) or via S3 (`false`) |
| `--ipv4Fallback` | `False` | Fall back to IPv4-only DNS when AAAA queries return SERVFAIL. Enable if the agent fails to connect to internal hostnames with `[Errno -3] Try again` |

---

## Flag Details

### `--verify`
Controls SSL certificate verification for all outbound HTTPS calls.

```bash
# Disable certificate verification (e.g. self-signed certs in internal environments)
--verify=false
```

**Default:** `false` — verification is disabled by default for compatibility with internal PKI.

---

### `--debugMode`
Enables DEBUG-level log output. Useful for diagnosing connection failures or unexpected task behaviour.

```bash
--debugMode=true
```

---

### `--enableStdoutLogging`
Prints log output to stdout in addition to rotating log files under `/tmp/armorcode/log/`.
Useful when running in Docker with log aggregation (e.g. CloudWatch, Datadog).

```bash
--enableStdoutLogging=true
```

---

### `--index`
Appends a suffix to the agent's log file name. Use when running multiple agent instances on the same host to avoid log file collisions.

```bash
# Produces: app_log_agent1.log
--index=_agent1
```

---

### `--poolSize`
Controls how many tasks the agent processes concurrently using gevent greenlets.
Increase for high-throughput environments; decrease if the agent is CPU- or memory-constrained.

```bash
--poolSize=10
```

---

### `--rateLimitPerMin`
Throttles outbound API calls to ArmorCode to at most N requests per minute.
Reduce if you see `429 Rate Limit` responses.

```bash
--rateLimitPerMin=100
```

---

### `--connectTimeout`
TCP connection timeout (seconds) for calls the agent makes to internal tools (e.g. JIRA, Coverity).
Does not affect calls to the ArmorCode server.

```bash
--connectTimeout=30
```

---

### `--readTimeoutSeconds`
Read (response body) timeout for calls to the ArmorCode server. Increase for slow networks or large task payloads.

```bash
--readTimeoutSeconds=300
```

---

### `--metricsRetentionDays`
Number of days to keep rotating metrics log files under `/tmp/armorcode/log/metrics/`.

```bash
--metricsRetentionDays=14
```

---

### `--uploadToAc`
Controls where large responses (> 500 KB) are uploaded.

- `true` (default) — upload directly to the ArmorCode server
- `false` — upload to S3 via a pre-signed URL

```bash
--uploadToAc=false
```

---

### `--ipv4Fallback`
Enables an IPv4-only DNS fallback for environments where the upstream DNS returns
`SERVFAIL` for AAAA (IPv6) queries. Without this flag, Python's resolver fails
entirely on dual-stack lookups when AAAA returns SERVFAIL — even if the A record
resolves correctly.

**When to use:** Enable if the agent logs `[Errno -3] Try again` or
`Failed to resolve '<hostname>'` errors connecting to internal endpoints,
and `curl` to the same endpoint works fine.

**How it works:** On first DNS failure, the hostname is cached as IPv4-only for
1 hour. Subsequent lookups skip the AAAA query entirely. The cache auto-expires
so the agent self-heals if DNS is fixed upstream without requiring a restart.

```bash
--ipv4Fallback
```

> **Note:** This is an app-side workaround. The permanent fix is to configure
> the authoritative DNS to return `NOERROR` with zero AAAA records instead of
> `SERVFAIL`.

---

## Example: Full Production Command

```bash
docker run -d \
  -v mydata:/tmp/armorcode \
  armorcode/armorcode-web-agent \
  --serverUrl='https://web-agent.armorcode.com' \
  --apiKey='<api_key>' \
  --envName='production' \
  --poolSize=10 \
  --rateLimitPerMin=200 \
  --connectTimeout=20 \
  --readTimeoutSeconds=120 \
  --enableStdoutLogging=true \
  --metricsRetentionDays=14
```

## Example: With IPv4 Fallback (DNS SERVFAIL environments)

```bash
docker run -d \
  -v mydata:/tmp/armorcode \
  armorcode/armorcode-web-agent \
  --serverUrl='https://web-agent.armorcode.com' \
  --apiKey='<api_key>' \
  --ipv4Fallback
```
