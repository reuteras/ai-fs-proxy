# Agent Guide — ai-fs-proxy

Two-script Python project. No build system, no package beyond `requests` (server side).

## Files

- `fs_proxy_server.py` — runs on the machine with network access; polls `<queue>/requests/` for JSON request files, forwards them to an OpenAI-compatible API, writes responses to `<queue>/responses/`
- `fs_proxy_client.py` — runs on the air-gapped machine; exposes a local HTTP server that serializes incoming requests as files and polls for response files

## Running

```bash
# Server (network-connected machine)
pip install requests
python fs_proxy_server.py --queue-dir /path/to/queue --api-base http://ai-host:11434/v1

# Client (air-gapped machine)
python fs_proxy_client.py --queue-dir /path/to/queue --port 8080
```

## Key constants (tune if needed)

| Constant          | File   | Default | Purpose                                               |
|-------------------|--------|---------|-------------------------------------------------------|
| `POLL_INTERVAL`   | both   | `0.3s`  | Polling frequency for new files                       |
| `MAX_WORKERS`     | server | `4`     | ThreadPoolExecutor concurrency cap                    |
| `REQUEST_TIMEOUT` | server | `120s`  | Upstream API call timeout                             |
| `REQUEST_TIMEOUT` | client | `300s`  | Max wait for a response (overridable via `--timeout`) |

## File protocol

- Requests: `<queue>/requests/<uuid>.json` (written atomically via `.tmp` rename)
- Non-streaming responses: `<queue>/responses/<uuid>.json`
- Streaming responses: `<queue>/responses/<uuid>-meta.json` (status + headers), then `<uuid>-000000.json`, `<uuid>-000001.json` … `<uuid>-done.json`
- Errors from the server: HTTP 502 response envelope written to `<queue>/responses/<uuid>.json`

## Testing

No test suite. Quick smoke test with curl against a running client:

```bash
curl http://127.0.0.1:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"llama3","messages":[{"role":"user","content":"Hello"}]}'
```
