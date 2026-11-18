"""Shared HTTP instrumentation. No third-party dependencies required."""

import json
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def log(service, event, **fields):
    print(json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(),
                      "service": service, "event": event, **fields}), flush=True)


class Metrics:
    bounds = (0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5)

    def __init__(self, service):
        self.service = service
        self.lock = threading.Lock()
        self.success = self.failure = 0
        self.duration_sum = 0.0
        self.buckets = [0] * len(self.bounds)

    def record(self, status, elapsed):
        with self.lock:
            if 200 <= status < 400:
                self.success += 1
            else:
                self.failure += 1
            self.duration_sum += elapsed
            for i, bound in enumerate(self.bounds):
                self.buckets[i] += elapsed <= bound

    def render(self):
        with self.lock:
            label = f'service="{self.service}"'
            count = self.success + self.failure
            rows = ["# HELP app_requests_total Completed business requests by outcome.",
                    "# TYPE app_requests_total counter",
                    f'app_requests_total{{{label},outcome="success"}} {self.success}',
                    f'app_requests_total{{{label},outcome="failure"}} {self.failure}',
                    "# HELP app_request_duration_seconds Business request duration in seconds.",
                    "# TYPE app_request_duration_seconds histogram"]
            rows += [f'app_request_duration_seconds_bucket{{{label},le="{bound}"}} {value}'
                     for bound, value in zip(self.bounds, self.buckets)]
            rows += [f'app_request_duration_seconds_bucket{{{label},le="+Inf"}} {count}',
                     f'app_request_duration_seconds_count{{{label}}} {count}',
                     f'app_request_duration_seconds_sum{{{label}}} {self.duration_sum:.6f}']
            return "\n".join(rows) + "\n"


class Handler(BaseHTTPRequestHandler):
    # HTTP/1.0 closes each connection; ThreadingHTTPServer keeps delayed work
    # from blocking health checks and other requests.
    def log_message(self, *_):
        pass

    def send(self, status, body, content_type="application/json"):
        payload = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        if getattr(self, "request_id", None):
            self.send_header("X-Request-ID", self.request_id)
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path == "/health":
            self.send(200, {"status": "ok", "service": self.server.service})
        elif self.path == "/metrics":
            self.send(200, self.server.metrics.render(), "text/plain; version=0.0.4")
        else:
            self.send(404, {"error": "not_found"})

    def do_POST(self):
        candidate = self.headers.get("X-Request-ID", "")
        self.request_id = (candidate if re.fullmatch(r"[A-Za-z0-9._-]{1,128}", candidate)
                           else str(uuid.uuid4()))
        started = time.monotonic()
        service = self.server.service
        log(service, "request_started", request_id=self.request_id,
            method="POST", path=self.path)
        status = 500
        try:
            if self.path != self.server.business_path:
                status, body = 404, {"error": "not_found"}
            else:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 16384:
                    raise ValueError("body must be 1–16384 bytes")
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict) or not isinstance(data.get("order_id"), str) or not data["order_id"]:
                    raise ValueError("order_id must be a nonempty string")
                status, body = self.process(data)
            body["request_id"] = self.request_id
            self.send(status, body)
        except (ValueError, UnicodeDecodeError) as exc:
            status = 400
            self.send(status, {"error": "invalid_request", "detail": str(exc),
                               "request_id": self.request_id})
        except (BrokenPipeError, ConnectionResetError):
            # Payment can finish after Checkout has already timed out.
            status = 499
            log(service, "client_disconnected", request_id=self.request_id)
        except Exception as exc:
            status = 500
            log(service, "internal_error", request_id=self.request_id, error=str(exc))
            try:
                self.send(status, {"error": "internal_error", "request_id": self.request_id})
            except OSError:
                pass
        finally:
            elapsed = time.monotonic() - started
            self.server.metrics.record(status, elapsed)
            log(service, "request_completed", request_id=self.request_id,
                status=status, duration_ms=round(elapsed * 1000, 3))


def serve(service, port, handler, path, **config):
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.service = service
    server.business_path = path
    server.metrics = Metrics(service)
    for key, value in config.items():
        setattr(server, key, value)
    log(service, "service_started", port=port, **config)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
