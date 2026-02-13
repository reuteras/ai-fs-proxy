#!/usr/bin/env python3
"""
fs_proxy_client.py — Filesystem Proxy Client (runs on the DFIR sandbox)

Exposes a local OpenAI-compatible HTTP API on localhost.
Your AI agent talks to this as if it were a normal LLM endpoint.
Requests are serialized to files on a shared drive, picked up by the
proxy server, and responses are written back as files.

Usage:
    python fs_proxy_client.py --queue-dir H:\queue --port 8080
"""

import argparse
import http.server
import json
import os
import time
import uuid
import threading
import logging
from pathlib import Path
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [CLIENT] %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DEFAULT_QUEUE_DIR = r"H:\queue"
DEFAULT_PORT = 8080
POLL_INTERVAL = 0.3        # seconds between checking for response file
REQUEST_TIMEOUT = 300       # seconds before giving up on a response
CLEANUP_AFTER = True        # delete request/response files after use


class FileSystemProxy:
    """Handles writing requests and polling for responses on the shared drive."""

    def __init__(self, queue_dir: str):
        self.queue_dir = Path(queue_dir)
        self.requests_dir = self.queue_dir / "requests"
        self.responses_dir = self.queue_dir / "responses"

        # Create directories if they don't exist
        self.requests_dir.mkdir(parents=True, exist_ok=True)
        self.responses_dir.mkdir(parents=True, exist_ok=True)
        log.info(f"Queue directory: {self.queue_dir}")

    def send_request(self, method: str, path: str, headers: dict, body: bytes | None) -> dict:
        """Write a request file and wait for the response file."""
        request_id = str(uuid.uuid4())

        # Build request envelope
        request_data = {
            "id": request_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "method": method,
            "path": path,
            "headers": headers,
            "body": body.decode("utf-8") if body else None,
        }

        # Write request file (write to .tmp first, then rename for atomicity)
        req_file = self.requests_dir / f"{request_id}.json"
        tmp_file = self.requests_dir / f"{request_id}.tmp"

        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(request_data, f)
        tmp_file.rename(req_file)

        log.info(f"Request {request_id} written — {method} {path}")

        # Poll for response
        resp_file = self.responses_dir / f"{request_id}.json"
        start = time.monotonic()

        while time.monotonic() - start < REQUEST_TIMEOUT:
            if resp_file.exists():
                # Small delay to ensure the file is fully written
                time.sleep(0.1)
                try:
                    with open(resp_file, "r", encoding="utf-8") as f:
                        response_data = json.load(f)

                    elapsed = time.monotonic() - start
                    log.info(f"Response {request_id} received in {elapsed:.1f}s")

                    if CLEANUP_AFTER:
                        try:
                            req_file.unlink(missing_ok=True)
                            resp_file.unlink(missing_ok=True)
                        except OSError:
                            pass

                    return response_data
                except (json.JSONDecodeError, OSError):
                    # File might still be written, retry
                    time.sleep(0.2)
                    continue

            time.sleep(POLL_INTERVAL)

        log.error(f"Request {request_id} timed out after {REQUEST_TIMEOUT}s")
        # Clean up orphaned request
        req_file.unlink(missing_ok=True)
        return {
            "status_code": 504,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"error": {"message": "Filesystem proxy timeout", "type": "timeout"}}),
        }


class ProxyHTTPHandler(http.server.BaseHTTPRequestHandler):
    """HTTP handler that forwards all requests through the filesystem proxy."""

    proxy: FileSystemProxy  # set on the class before serving

    def do_request(self):
        # Read body if present
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else None

        # Collect headers (skip hop-by-hop)
        skip_headers = {"host", "connection", "transfer-encoding", "keep-alive"}
        headers = {
            k: v
            for k, v in self.headers.items()
            if k.lower() not in skip_headers
        }

        # Send through filesystem
        response = self.proxy.send_request(
            method=self.command,
            path=self.path,
            headers=headers,
            body=body,
        )

        # Write HTTP response back to the AI agent
        status = response.get("status_code", 500)
        resp_headers = response.get("headers", {})
        resp_body = response.get("body", "")

        self.send_response(status)
        for k, v in resp_headers.items():
            if k.lower() not in ("transfer-encoding", "connection"):
                self.send_header(k, v)
        self.end_headers()

        if isinstance(resp_body, str):
            self.wfile.write(resp_body.encode("utf-8"))
        elif isinstance(resp_body, bytes):
            self.wfile.write(resp_body)

    # Handle all HTTP methods
    do_GET = do_request
    do_POST = do_request
    do_PUT = do_request
    do_DELETE = do_request
    do_PATCH = do_request
    do_OPTIONS = do_request

    def log_message(self, format, *args):
        log.info(f"HTTP {args[0] if args else ''}")


class StreamingProxyHTTPHandler(ProxyHTTPHandler):
    """Extended handler with support for streaming (SSE) responses."""

    def do_request(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else None

        # Check if this is a streaming request
        is_streaming = False
        if body:
            try:
                req_json = json.loads(body)
                is_streaming = req_json.get("stream", False)
            except json.JSONDecodeError:
                pass

        skip_headers = {"host", "connection", "transfer-encoding", "keep-alive"}
        headers = {
            k: v for k, v in self.headers.items()
            if k.lower() not in skip_headers
        }

        if not is_streaming:
            # Non-streaming: use normal flow
            response = self.proxy.send_request(
                method=self.command, path=self.path,
                headers=headers, body=body,
            )
            status = response.get("status_code", 500)
            resp_headers = response.get("headers", {})
            resp_body = response.get("body", "")
            self.send_response(status)
            for k, v in resp_headers.items():
                if k.lower() not in ("transfer-encoding", "connection"):
                    self.send_header(k, v)
            self.end_headers()
            if isinstance(resp_body, str):
                self.wfile.write(resp_body.encode("utf-8"))
            return

        # Streaming: write request, then poll for chunk files
        request_id = str(uuid.uuid4())
        request_data = {
            "id": request_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "method": self.command,
            "path": self.path,
            "headers": headers,
            "body": body.decode("utf-8") if body else None,
            "stream": True,
        }
        req_file = self.proxy.requests_dir / f"{request_id}.json"
        tmp_file = self.proxy.requests_dir / f"{request_id}.tmp"
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(request_data, f)
        tmp_file.rename(req_file)
        log.info(f"Streaming request {request_id} written")

        # Send SSE headers
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        # Poll for stream chunk files: resp-<id>-<seq>.json, resp-<id>-done.json
        seq = 0
        start = time.monotonic()
        while time.monotonic() - start < REQUEST_TIMEOUT:
            # Check for next chunk
            chunk_file = self.proxy.responses_dir / f"{request_id}-{seq:06d}.json"
            done_file = self.proxy.responses_dir / f"{request_id}-done.json"

            if chunk_file.exists():
                time.sleep(0.05)
                try:
                    with open(chunk_file, "r", encoding="utf-8") as f:
                        chunk_data = f.read()
                    self.wfile.write(f"data: {chunk_data}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    chunk_file.unlink(missing_ok=True)
                    seq += 1
                    start = time.monotonic()  # reset timeout on activity
                except OSError:
                    time.sleep(0.1)
                continue

            if done_file.exists():
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                done_file.unlink(missing_ok=True)
                req_file.unlink(missing_ok=True)
                log.info(f"Stream {request_id} complete ({seq} chunks)")
                return

            time.sleep(POLL_INTERVAL)

        log.error(f"Stream {request_id} timed out")

    do_GET = do_request
    do_POST = do_request
    do_PUT = do_request
    do_DELETE = do_request
    do_PATCH = do_request
    do_OPTIONS = do_request


def main():
    parser = argparse.ArgumentParser(description="Filesystem Proxy Client")
    parser.add_argument("--queue-dir", default=DEFAULT_QUEUE_DIR, help="Shared drive queue directory")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Local port to listen on")
    parser.add_argument("--timeout", type=int, default=REQUEST_TIMEOUT, help="Response timeout in seconds")
    parser.add_argument("--streaming", action="store_true", help="Enable SSE streaming support")
    args = parser.parse_args()

    global REQUEST_TIMEOUT
    REQUEST_TIMEOUT = args.timeout

    proxy = FileSystemProxy(args.queue_dir)

    handler_class = StreamingProxyHTTPHandler if args.streaming else ProxyHTTPHandler
    handler_class.proxy = proxy

    server = http.server.HTTPServer(("127.0.0.1", args.port), handler_class)
    log.info(f"Listening on http://127.0.0.1:{args.port}")
    log.info(f"Configure your AI agent with: OPENAI_API_BASE=http://127.0.0.1:{args.port}/v1")
    log.info("Press Ctrl+C to stop")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
