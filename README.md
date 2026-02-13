# Filesystem HTTP Proxy for Air-Gapped AI Access

Tunnels OpenAI-compatible API requests through a shared filesystem (e.g. a mapped network drive) between two Windows machines.

## Architecture

```
┌─────────────────────┐                          ┌─────────────────────┐
│   DFIR Sandbox       │                          │   Proxy Machine     │
│                      │    H:\queue\requests\    │                     │
│   AI Agent           │    ─────────────────▶    │   fs_proxy_server   │──▶ AI Model API
│     ▼                │                          │                     │
│   localhost:8080     │    H:\queue\responses\   │                     │
│     ▼                │    ◀─────────────────    │                     │
│   fs_proxy_client    │                          │                     │
└─────────────────────┘                          └─────────────────────┘
```

## Requirements

- Python 3.10+
- **Client (sandbox):** no external packages needed (stdlib only)
- **Server (proxy machine):** `pip install requests`

## Quick Start

### 1. On the proxy machine (has network access to the AI model)

```cmd
pip install requests

python fs_proxy_server.py --queue-dir H:\queue --api-base http://ai-model-host:11434/v1
```

If your API requires a key:
```cmd
python fs_proxy_server.py --queue-dir H:\queue --api-base http://ai-model-host:11434/v1 --api-key sk-your-key
:: or
set OPENAI_API_KEY=sk-your-key
python fs_proxy_server.py --queue-dir H:\queue --api-base http://ai-model-host:11434/v1
```

### 2. On the DFIR sandbox (no network)

```cmd
python fs_proxy_client.py --queue-dir H:\queue --port 8080
```

For streaming support (SSE):
```cmd
python fs_proxy_client.py --queue-dir H:\queue --port 8080 --streaming
```

### 3. Point your AI agent at the local proxy

```cmd
set OPENAI_API_BASE=http://127.0.0.1:8080/v1
set OPENAI_API_KEY=not-needed
```

Or configure your agent directly with the base URL `http://127.0.0.1:8080/v1`.

## Testing

Quick test from the sandbox with curl:

```cmd
curl http://127.0.0.1:8080/v1/chat/completions -H "Content-Type: application/json" -d "{\"model\": \"llama3\", \"messages\": [{\"role\": \"user\", \"content\": \"Hello\"}]}"
```

## How It Works

1. The client exposes a local HTTP server that your AI agent talks to
2. Each incoming HTTP request is serialized as a JSON file in `H:\queue\requests\`
3. The server watches that directory, picks up new files, and makes the real HTTP call
4. The response is written as a JSON file in `H:\queue\responses\`
5. The client polls for the response file and returns it to the AI agent

### File format

**Request** (`H:\queue\requests\<uuid>.json`):
```json
{
  "id": "550e8400-...",
  "timestamp": "2025-02-13T10:30:00+00:00",
  "method": "POST",
  "path": "/v1/chat/completions",
  "headers": {"Content-Type": "application/json"},
  "body": "{\"model\": \"llama3\", ...}"
}
```

**Response** (`H:\queue\responses\<uuid>.json`):
```json
{
  "id": "550e8400-...",
  "status_code": 200,
  "headers": {"Content-Type": "application/json"},
  "body": "{\"choices\": [...]}"
}
```

### Atomicity

Files are written to `.tmp` first and then renamed. This prevents the other side from reading a partially written file. This is safe on both NTFS and SMB shares.

## Options

### Client

| Flag | Default | Description |
|------|---------|-------------|
| `--queue-dir` | `H:\queue` | Shared drive queue directory |
| `--port` | `8080` | Local port to listen on |
| `--timeout` | `300` | Max seconds to wait for a response |
| `--streaming` | off | Enable SSE streaming support |

### Server

| Flag | Default | Description |
|------|---------|-------------|
| `--queue-dir` | `H:\queue` | Shared drive queue directory |
| `--api-base` | `http://localhost:11434/v1` | Upstream AI model API URL |
| `--api-key` | `$OPENAI_API_KEY` | API key for the upstream API |
| `--cleanup-interval` | `300` | Seconds between stale file cleanup |

## Troubleshooting

**Requests time out:** Check that both machines can read/write `H:\queue`. Verify the server is running and the API base URL is correct.

**Permission errors on shared drive:** Make sure both user accounts have read/write access to the queue directory.

**Slow responses:** SMB network shares add latency. The poll interval (0.3s) adds some overhead. For lower latency, reduce `POLL_INTERVAL` in both scripts (at the cost of higher CPU/IO).

**Stale files accumulate:** The server auto-cleans files older than 1 hour. You can also manually delete everything in `H:\queue\requests` and `H:\queue\responses`.
