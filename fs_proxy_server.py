#!/usr/bin/env python3
r"""
fs_proxy_server.py — Filesystem Proxy Server (runs on the machine with network access)

Watches the shared drive for request files, forwards them to the actual
AI model API (OpenAI-compatible), and writes responses back as files.

Usage:
    python fs_proxy_server.py --queue-dir H:\queue --api-base http://ai-model:11434/v1
    python fs_proxy_server.py --queue-dir H:\queue --api-base http://ai-model:11434/v1 --api-key sk-...
    python fs_proxy_server.py --queue-dir H:\queue --api-base https://ai-model/v1 --ignore-cert-errors

Multiple upstreams (routed by the first path segment, e.g. /ollama/v1/...):
    python fs_proxy_server.py --queue-dir H:\queue \
        --upstream ollama=http://gpu-box:11434/v1 \
        --upstream vllm=https://vllm-host/v1
    API keys per upstream come from FS_PROXY_KEY_<NAME> (e.g. FS_PROXY_KEY_VLLM).

Client-chosen URLs (the client's /fwd/<scheme>/<host>/... paths), limited to prefixes:
    python fs_proxy_server.py --queue-dir H:\queue --allow-url https://llm.example.com/
"""

import argparse
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

import requests
import urllib3

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
POLL_INTERVAL = 0.3  # seconds between scanning for new requests
MAX_WORKERS = 4  # concurrent request handlers
REQUEST_TIMEOUT = 120  # timeout for upstream HTTP requests


def join_url(api_base: str, path: str) -> str:
    """Join base and path, collapsing a duplicated segment such as /v1 + /v1/..."""
    base = api_base.rstrip("/")
    if not path.startswith("/"):
        path = "/" + path
    base_last = "/" + base.rsplit("/", 1)[-1]
    if path == base_last or path.startswith((base_last + "/", base_last + "?")):
        path = path[len(base_last) :]
    return f"{base}{path}"


def url_allowed(url: str, prefixes: list[str]) -> bool:
    """Check a full URL against allowlist prefixes.

    Scheme and host[:port] must match exactly and the path must start at a
    segment boundary of the prefix path, so https://a.example does not match
    https://a.example.evil or https://a.example:8443. A prefix with only a
    scheme (e.g. "https://") allows every host for that scheme.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return False
    if "@" in parts.netloc or "\\" in url:
        return False
    if any(seg in (".", "..") for seg in unquote(parts.path).split("/")):
        return False
    for prefix in prefixes:
        pre = urlsplit(prefix)
        if pre.scheme != parts.scheme:
            continue
        if not pre.netloc:
            return True
        if pre.netloc.lower() != parts.netloc.lower():
            continue
        pre_path = pre.path.rstrip("/")
        if (
            not pre_path
            or parts.path == pre_path
            or parts.path.startswith(pre_path + "/")
        ):
            return True
    return False


def key_env_name(name: str) -> str:
    return "FS_PROXY_KEY_" + re.sub(r"[^A-Za-z0-9]", "_", name).upper()


class FileSystemProxyServer:
    """Watches for request files and forwards them to the AI model API."""

    def __init__(
        self,
        queue_dir: str,
        api_base: str | None,
        api_key: str | None = None,
        ignore_cert_errors: bool = False,
        upstreams: dict[str, dict] | None = None,
        allow_urls: list[str] | None = None,
    ):
        self.queue_dir = Path(queue_dir)
        self.requests_dir = self.queue_dir / "requests"
        self.responses_dir = self.queue_dir / "responses"
        self.api_base = api_base.rstrip("/") if api_base else None
        self.api_key = api_key
        # name -> {"api_base": str, "api_key": str | None}
        self.upstreams = upstreams or {}
        # URL prefixes the client may target directly via request["url"]
        self.allow_urls = allow_urls or []

        # Track processed files to avoid double-processing
        self.processed: set[str] = set()
        self.lock = threading.Lock()
        self.executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)

        # Create dirs
        self.requests_dir.mkdir(parents=True, exist_ok=True)
        self.responses_dir.mkdir(parents=True, exist_ok=True)

        # Session for connection pooling
        self.session = requests.Session()

        if ignore_cert_errors:
            self.session.verify = False
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            log.warning("TLS certificate verification is disabled")

        log.info(f"Queue directory: {self.queue_dir}")
        for name, up in self.upstreams.items():
            key_info = "with key" if up.get("api_key") else "no key"
            log.info(f"Upstream /{name} -> {up['api_base']} ({key_info})")
        if self.api_base:
            log.info(f"Default API base: {self.api_base}")
        for prefix in self.allow_urls:
            log.info(f"Client URLs allowed under: {prefix}")

    def resolve(self, path: str) -> tuple[str, str | None] | None:
        """Map a request path to (upstream URL, api key)."""
        segment, _, rest = path.lstrip("/").partition("/")
        name = segment.split("?", 1)[0]
        if name in self.upstreams:
            up = self.upstreams[name]
            remainder = "/" + rest if rest else path[len(name) + 1 :] or "/"
            return join_url(up["api_base"], remainder), up.get("api_key")
        if self.api_base:
            return join_url(self.api_base, path), self.api_key
        return None

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
        full_url = request_data.get("url")

        if full_url:
            # Client-chosen URL: only allowlisted, no server key, no redirects
            if not url_allowed(full_url, self.allow_urls):
                log.warning(f"Rejected {request_id}: URL not allowed: {full_url}")
                reason = (
                    "not in the server's --allow-url list"
                    if self.allow_urls
                    else "disabled (server has no --allow-url)"
                )
                self._write_error_response(
                    request_id,
                    f"Forwarding to {full_url} is {reason}",
                    status_code=403,
                )
                return
            resolved = (full_url, None)
        else:
            resolved = self.resolve(path)
        if resolved is None:
            log.error(f"No upstream for {request_id}: {path}")
            known = ", ".join(f"/{n}" for n in self.upstreams) or "(none)"
            self._write_error_response(
                request_id,
                f"No upstream configured for path {path}; known prefixes: {known}",
                status_code=404,
            )
            return
        url, api_key = resolved
        if api_key:
            # Override whatever placeholder key the agent sent
            headers = {k: v for k, v in headers.items() if k.lower() != "authorization"}
            headers["Authorization"] = f"Bearer {api_key}"

        log.info(f"Processing {request_id}: {method} {url} (stream={is_streaming})")

        # A redirect could lead away from an allowlisted URL, so don't follow
        redirects = not full_url
        try:
            if is_streaming:
                self._handle_streaming(
                    request_id, method, url, headers, body, redirects
                )
            else:
                self._handle_normal(request_id, method, url, headers, body, redirects)
        except Exception as e:
            log.exception(f"Error processing {request_id}")
            self._write_error_response(request_id, str(e))

    def _handle_normal(
        self,
        request_id: str,
        method: str,
        url: str,
        headers: dict,
        body: str | None,
        redirects: bool = True,
    ):
        """Handle a normal (non-streaming) request."""
        response = self.session.request(
            method=method,
            url=url,
            headers=headers,
            data=body.encode("utf-8") if body else None,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=redirects,
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

    def _handle_streaming(
        self,
        request_id: str,
        method: str,
        url: str,
        headers: dict,
        body: str | None,
        redirects: bool = True,
    ):
        """Handle a streaming (SSE) request by writing chunk files."""
        response = self.session.request(
            method=method,
            url=url,
            headers=headers,
            data=body.encode("utf-8") if body else None,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=redirects,
            stream=True,
        )

        # Write meta file first so the client knows the real status code.
        # If upstream did not answer with SSE (typically an error), include
        # the body so the client can return it as a normal response.
        meta = {
            "status_code": response.status_code,
            "headers": dict(response.headers),
        }
        is_sse = "text/event-stream" in response.headers.get("Content-Type", "")
        if not is_sse:
            meta["body"] = response.text
        meta_file = self.responses_dir / f"{request_id}-meta.json"
        tmp_file = self.responses_dir / f"{request_id}-meta.tmp"
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(meta, f)
        tmp_file.rename(meta_file)
        if not is_sse:
            log.info(
                f"Stream {request_id} got non-SSE reply (HTTP {response.status_code})"
            )

        # Still write the done file for a non-SSE reply, so older clients
        # (which ignore the meta body) finish instead of waiting for chunks
        seq = 0
        lines = response.iter_lines(decode_unicode=True) if is_sse else []
        for line in lines:
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

    def _write_error_response(
        self, request_id: str, error_msg: str, status_code: int = 502
    ):
        """Write an error response file."""
        response_data = {
            "id": request_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status_code": status_code,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(
                {
                    "error": {
                        "message": f"Proxy error: {error_msg}",
                        "type": "proxy_error",
                    }
                }
            ),
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

                self.executor.submit(self.process_request, req_file)
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
    parser.add_argument(
        "--queue-dir", default=DEFAULT_QUEUE_DIR, help="Shared drive queue directory"
    )
    parser.add_argument(
        "--api-base",
        default=None,
        help=f"Default AI model API base URL (default: {DEFAULT_API_BASE} "
        "unless --upstream is given)",
    )
    parser.add_argument(
        "--upstream",
        action="append",
        default=[],
        metavar="NAME=URL",
        help="Named upstream, reached via /NAME/... (repeatable). "
        "API key is read from FS_PROXY_KEY_<NAME>",
    )
    parser.add_argument(
        "--allow-url",
        action="append",
        default=[],
        metavar="PREFIX",
        help="Allow the client to target URLs under this prefix directly "
        "(repeatable), e.g. https://llm.example.com/ or https:// for any host",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("OPENAI_API_KEY"),
        help="API key (or set OPENAI_API_KEY env var)",
    )
    parser.add_argument(
        "--cleanup-interval",
        type=int,
        default=300,
        help="Stale file cleanup interval in seconds",
    )
    parser.add_argument(
        "--ignore-cert-errors",
        action="store_true",
        help="Disable TLS certificate verification for upstream API requests",
    )
    args = parser.parse_args()

    upstreams: dict[str, dict] = {}
    for spec in args.upstream:
        name, sep, url = spec.partition("=")
        if not sep or not name or not url:
            parser.error(f"--upstream must be NAME=URL, got {spec!r}")
        upstreams[name] = {
            "api_base": url.rstrip("/"),
            "api_key": os.environ.get(key_env_name(name)),
        }

    api_base = args.api_base
    if api_base is None and not upstreams:
        api_base = DEFAULT_API_BASE

    server = FileSystemProxyServer(
        queue_dir=args.queue_dir,
        api_base=api_base,
        api_key=args.api_key,
        ignore_cert_errors=args.ignore_cert_errors,
        upstreams=upstreams,
        allow_urls=args.allow_url,
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
