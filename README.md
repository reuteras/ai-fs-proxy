# Filesystem HTTP Proxy for Air-Gapped AI Access

Tunnels OpenAI-compatible API requests through a shared filesystem (e.g. a mapped network drive) between two Windows machines.

## Architecture

```text
┌─────────────────────┐                          ┌─────────────────────┐
│   DFIR Sandbox      │                          │   Proxy Machine     │
│                     │    H:\queue\requests\    │                     │
│   AI Agent          │    ─────────────────▶    │   fs_proxy_server   │──▶ AI Model API
│     ▼               │                          │                     │
│   localhost:8080    │    H:\queue\responses\   │                     │
│     ▼               │    ◀─────────────────    │                     │
│   fs_proxy_client   │                          │                     │
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

## Multiple upstream hosts (e.g. for opencode)

The server can route to several upstream APIs. Each one gets a name, and requests are routed by the first path segment: `/<name>/v1/...` goes to that upstream (the prefix is stripped before forwarding).

```cmd
set FS_PROXY_KEY_CLOUD=sk-your-key
python fs_proxy_server.py --queue-dir H:\queue ^
    --upstream ollama=http://gpu-box:11434/v1 ^
    --upstream cloud=https://llm.example.com/v1
```

- The API key for upstream `<name>` is read from the environment variable `FS_PROXY_KEY_<NAME>` (uppercased, non-alphanumerics become `_`). When set, it replaces any `Authorization` header the agent sends, so the agent can use a dummy key.
- `--api-base` still works and acts as the fallback for paths that don't match an upstream name. Without `--api-base` and with at least one `--upstream`, unmatched paths get HTTP 404.
- A duplicated `/v1` (base ending in `/v1` + path starting with `/v1/`) is collapsed.

On the sandbox, run the client with `--streaming` (opencode streams its responses):

```cmd
python fs_proxy_client.py --queue-dir H:\queue --port 8080 --streaming
```

Then define one opencode provider per upstream in `opencode.json`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "ollama": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Ollama (via fs-proxy)",
      "options": { "baseURL": "http://127.0.0.1:8080/ollama/v1", "apiKey": "not-needed" },
      "models": { "qwen2.5-coder:32b": {} }
    },
    "cloud": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Cloud LLM (via fs-proxy)",
      "options": { "baseURL": "http://127.0.0.1:8080/cloud/v1", "apiKey": "not-needed" },
      "models": { "gpt-4o": {} }
    }
  }
}
```

## Client-chosen URLs (no server change per host)

Instead of naming every upstream on the server, the agent can put the full upstream URL in its base URL:

```text
http://127.0.0.1:8080/fwd/<scheme>/<host[:port]>/<path>
http://127.0.0.1:8080/fwd/https/llm.example.com/v1   ->  https://llm.example.com/v1
```

For security the server only forwards these to URLs matching an `--allow-url` prefix, set once when the server starts:

```cmd
python fs_proxy_server.py --queue-dir H:\queue ^
    --allow-url https://llm.example.com/v1 ^
    --allow-url http://10.0.0.5:11434/ ^
    --allow-url https://*.domain.com/v1
```

- Scheme and `host[:port]` must match exactly, and the path must start at a segment boundary (`/v1` allows `/v1/chat/completions` but not `/v10` or `/admin`). A default port (`:443`, `:80`) can be written or left out.
- `*.domain.com` matches any subdomain (`a.domain.com`, `a.b.domain.com`) but not `domain.com` itself; add that separately if needed. `--allow-url https://` allows any HTTPS host.
- Without `--allow-url`, `/fwd/...` requests are refused with HTTP 403. Without it, anything in the sandbox that can write to the queue could make the proxy machine call any address it can reach.
- No server-side API key is added for these requests: put the key in the agent config (opencode's `apiKey`), and the `Authorization` header is passed through.
- Redirects are not followed for these requests, so an allowed host cannot redirect the proxy elsewhere.

opencode provider example:

```json
"cloud": {
  "npm": "@ai-sdk/openai-compatible",
  "options": { "baseURL": "http://127.0.0.1:8080/fwd/https/llm.example.com/v1", "apiKey": "sk-your-key" },
  "models": { "gpt-4o": {} }
}
```

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

| Flag          | Default    | Description                        |
|---------------|------------|------------------------------------|
| `--queue-dir` | `H:\queue` | Shared drive queue directory       |
| `--port`      | `8080`     | Local port to listen on            |
| `--timeout`   | `300`      | Max seconds to wait for a response |
| `--streaming` | off        | Enable SSE streaming support       |

### Server

| Flag                   | Default                     | Description                                                |
|------------------------|-----------------------------|------------------------------------------------------------|
| `--queue-dir`          | `H:\queue`                  | Shared drive queue directory                               |
| `--api-base`           | `http://localhost:11434/v1` | Default upstream API URL (no default if `--upstream` set)  |
| `--upstream`           | none                        | `NAME=URL`, reached via `/NAME/...`; repeatable            |
| `--allow-url`          | none                        | URL prefix the client may target via `/fwd/...`; repeatable |
| `--api-key`            | `$OPENAI_API_KEY`           | API key for the upstream API                               |
| `--cleanup-interval`   | `300`                       | Seconds between stale file cleanup                         |
| `--ignore-cert-errors` | off                         | Disable TLS certificate verification for upstream requests |

## Troubleshooting

**Requests time out:** Check that both machines can read/write `H:\queue`. Verify the server is running and the API base URL is correct.

**Permission errors on shared drive:** Make sure both user accounts have read/write access to the queue directory.

**Slow responses:** SMB network shares add latency. The poll interval (0.3s) adds some overhead. For lower latency, reduce `POLL_INTERVAL` in both scripts (at the cost of higher CPU/IO).

**Stale files accumulate:** The server auto-cleans files older than 1 hour. You can also manually delete everything in `H:\queue\requests` and `H:\queue\responses`.
