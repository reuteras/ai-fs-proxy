#!/usr/bin/env python3
"""
fs_proxy_server.py — Filesystem Proxy Server (runs on the machine with network access)

Watches the shared drive for request files, forwards them to the actual
AI model API (OpenAI-compatible), and writes responses back as files.

Usage:
    python fs_proxy_server.py --queue-dir H:\queue --api-base http://ai-model:11434/v1
    python fs_proxy_server.py --queue-dir H:\queue --api-base http://ai-model:11434/v1 --api-key sk-...
"""

import argparse
import json
import os
import time
import logging
import threading
import requests
from pathlib import Path
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [SERVER] %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DEFAULT_QUEUE_DIR = r"H:\queue"
DEFAULT_API_BASE = "http://localhost:11434/v1"
POLL_INTERVAL = 0.3          # seconds between scanning for new requests
MAX_WORKERS = 4              # concurrent request handlers
REQUEST_TIMEOUT = 120        # timeout for upstream HTTP requests


class FileSystemProxyServer:
    """Watches for request files and forwards them to the AI model API."""

    def __init__(self, queue_dir: str, api_base: str, api_key: str | None = None):
        self.queue_dir = Path(queue_dir)
        self.requests_dir = self.queue_dir / "requests"
        self.responses_dir = self.queue_dir / "responses"
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key

        # Track processed files to avoid double-processing
        self.processed: set[str] = set()
        self.lock = threading.Lock()

        # Create dirs
        self.requests_dir.mkdir(parents=True, exist_ok=True)
        self.responses_dir.mkdir(parents=True, exist_ok=True)

        # Session for connection pooling
        self.session = requests.Session()
        if self.api_key:
            self.session.headers["Authorization"] = f"Bearer {self.api_key}"

        log.info(f"Queue directory: {self.queue_dir}")
        log.info(f"API base: {self.api_base}")

    def process_request(self, req_file: Path):
        """Process a single request file."""
        try:
            with open(req_file, "r", encoding="utf-8") as f:
                request_data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            log.error(f"Failed to read {req_file.name}: {e}")
            return

        request_id = request_data["id"]
        method = request_data.get("method", "POST").upper()
        path = request_data.get("path", "/")
        headers = request_data.get("headers", {})
        body = request_data.get("body")
        is_streaming = request_data.get("stream", False)

        # Build upstream URL
        url = f"{self.api_base}{path}"
        log.info(f"Processing {request_id}: {method} {url} (stream={is_streaming})")

        try:
            if is_streaming:
                self._handle_streaming(request_id, method, url, headers, body)
            else:
                self._handle_normal(request_id, method, url, headers, body)
        except Exception as e:
            log.error(f"Error processing {request_id}: {e}")
            self._write_error_response(request_id, str(e))

    def _handle_normal(self, request_id: str, method: str, url: str, headers: dict, body: str | None):
        """Handle a normal (non-streaming) request."""
        response = self.session.request(
            method=method,
            url=url,
            headers=headers,
            data=body.encode("utf-8") if body else None,
            timeout=REQUEST_TIMEOUT,
        )

        # Build response envelope
        response_data = {
            "id": request_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status_code": response.status_code,
            "headers": dict(response.headers),
            "body": response.text,
        }

        # Write response (atomic via rename)
        resp_file = self.responses_dir / f"{request_id}.json"
        tmp_file = self.responses_dir / f"{request_id}.tmp"
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(response_data, f)
        tmp_file.rename(resp_file)

        log.info(f"Response {request_id} written (HTTP {response.status_code})")

    def _handle_streaming(self, request_id: str, method: str, url: str, headers: dict, body: str | None):
        """Handle a streaming (SSE) request by writing chunk files."""
        response = self.session.request(
            method=method,
            url=url,
            headers=headers,
            data=body.encode("utf-8") if body else None,
            timeout=REQUEST_TIMEOUT,
            stream=True,
        )

        seq = 0
        for line in response.iter_lines(decode_unicode=True):
            if not line:
                continue
            if line.startswith("data: "):
                data = line[6:]
                if data.strip() == "[DONE]":
                    break

                # Write chunk file
                chunk_file = self.responses_dir / f"{request_id}-{seq:06d}.json"
                tmp_file = self.responses_dir / f"{request_id}-{seq:06d}.tmp"
                with open(tmp_file, "w", encoding="utf-8") as f:
                    f.write(data)
                tmp_file.rename(chunk_file)
                seq += 1

        # Signal completion
        done_file = self.responses_dir / f"{request_id}-done.json"
        tmp_file = self.responses_dir / f"{request_id}-done.tmp"
        with open(tmp_file, "w", encoding="utf-8") as f:
            f.write("{}")
        tmp_file.rename(done_file)

        log.info(f"Stream {request_id} complete ({seq} chunks)")

    def _write_error_response(self, request_id: str, error_msg: str):
        """Write an error response file."""
        response_data = {
            "id": request_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status_code": 502,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({
                "error": {
                    "message": f"Proxy error: {error_msg}",
                    "type": "proxy_error",
                }
            }),
        }
        resp_file = self.responses_dir / f"{request_id}.json"
        tmp_file = self.responses_dir / f"{request_id}.tmp"
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(response_data, f)
        tmp_file.rename(resp_file)

    def scan_and_process(self):
        """Scan for new request files and process them."""
        try:
            for req_file in sorted(self.requests_dir.glob("*.json")):
                fname = req_file.name
                with self.lock:
                    if fname in self.processed:
                        continue
                    self.processed.add(fname)

                # Process in a thread
                thread = threading.Thread(
                    target=self.process_request,
                    args=(req_file,),
                    daemon=True,
                )
                thread.start()
        except OSError as e:
            log.error(f"Error scanning queue: {e}")

    def run(self):
        """Main loop: continuously scan for new requests."""
        log.info("Watching for requests... (Ctrl+C to stop)")
        try:
            while True:
                self.scan_and_process()
                time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            log.info("Shutting down")

    def cleanup_stale(self, max_age_seconds: int = 3600):
        """Clean up old request/response files."""
        now = time.time()
        for directory in [self.requests_dir, self.responses_dir]:
            for f in directory.glob("*"):
                try:
                    age = now - f.stat().st_mtime
                    if age > max_age_seconds:
                        f.unlink()
                        log.info(f"Cleaned up stale file: {f.name}")
                except OSError:
                    pass


def main():
    parser = argparse.ArgumentParser(description="Filesystem Proxy Server")
    parser.add_argument("--queue-dir", default=DEFAULT_QUEUE_DIR, help="Shared drive queue directory")
    parser.add_argument("--api-base", default=DEFAULT_API_BASE, help="AI model API base URL")
    parser.add_argument("--api-key", default=os.environ.get("OPENAI_API_KEY"), help="API key (or set OPENAI_API_KEY env var)")
    parser.add_argument("--cleanup-interval", type=int, default=300, help="Stale file cleanup interval in seconds")
    args = parser.parse_args()

    server = FileSystemProxyServer(
        queue_dir=args.queue_dir,
        api_base=args.api_base,
        api_key=args.api_key,
    )

    # Periodic cleanup thread
    def cleanup_loop():
        while True:
            time.sleep(args.cleanup_interval)
            server.cleanup_stale()

    cleanup_thread = threading.Thread(target=cleanup_loop, daemon=True)
    cleanup_thread.start()

    # Clean up anything stale from previous runs
    server.cleanup_stale()

    server.run()


if __name__ == "__main__":
    main()
