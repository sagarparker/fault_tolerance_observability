"""Run the real services through success, timeout, and recovery; save evidence."""

import argparse
import json
import os
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def call(url, data=None, request_id=None):
    headers = {"Content-Type": "application/json"}
    if request_id:
        headers["X-Request-ID"] = request_id
    req = Request(url, data=json.dumps(data).encode() if data is not None else None,
                  headers=headers)
    started = time.monotonic()
    try:
        response = urlopen(req, timeout=3)
    except HTTPError as exc:
        response = exc
    with response:
        body = response.read().decode()
        return response.status, body, (time.monotonic() - started) * 1000, response.headers


def run_demo(output):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    processes, handles, transcript, phases = [], [], [], []

    def note(message):
        print(message, flush=True)
        transcript.append(message)

    def require(condition, message):
        if not condition:
            raise AssertionError(message)

    def set_delay(path, seconds):
        temporary = path.with_suffix(".tmp")
        temporary.write_text(str(seconds))
        os.replace(temporary, path)

    try:
        with tempfile.TemporaryDirectory(prefix="checkout-lab-") as temp:
            delay_file = Path(temp) / "delay.txt"
            set_delay(delay_file, 0)
            payment_port, checkout_port = free_port(), free_port()
            while checkout_port == payment_port:
                checkout_port = free_port()
            payment_url = f"http://127.0.0.1:{payment_port}"
            checkout_url = f"http://127.0.0.1:{checkout_port}"
            commands = [
                ("payment", ["--port", str(payment_port), "--delay-file", str(delay_file)]),
                ("checkout", ["--port", str(checkout_port), "--payment-url", payment_url,
                              "--payment-timeout", "0.3"]),
            ]
            for name, args in commands:
                handle = (output / f"{name}.jsonl").open("w")
                handles.append(handle)
                processes.append(subprocess.Popen(
                    [sys.executable, "-m", f"services.{name}", *args], cwd=ROOT,
                    stdout=handle, stderr=subprocess.STDOUT))
            for process, url in zip(processes, [payment_url, checkout_url]):
                deadline = time.monotonic() + 5
                while True:
                    require(process.poll() is None, "service exited during startup; inspect JSONL logs")
                    try:
                        require(call(url + "/health")[0] == 200, "health check failed")
                        break
                    except URLError:
                        if time.monotonic() >= deadline:
                            raise AssertionError("service startup timed out")
                        time.sleep(0.05)
            note("Started Payment and Checkout. Checkout timeout=0.300s; injected delay=0.800s.")
            total_requests = total_errors = 0
            for phase, delay, count, expected in [
                ("success", 0, 3, 200), ("failure", 0.8, 3, 504), ("recovery", 0, 6, 200)
            ]:
                set_delay(delay_file, delay)
                note(f"\nPHASE {phase}: Payment delay={delay:.3f}s")
                latencies, errors = [], 0
                for index in range(1, count + 1):
                    request_id = f"{phase}-{index:02d}"
                    status, raw, latency, headers = call(
                        checkout_url + "/checkout", {"order_id": f"order-{request_id}"}, request_id)
                    body = json.loads(raw)
                    require(status == expected, f"{request_id}: expected {expected}, got {status}")
                    require(headers["X-Request-ID"] == body["request_id"] == request_id,
                            "Checkout must preserve request ID")
                    if status == 200:
                        require(body["payment"]["request_id"] == request_id,
                                "Payment must receive the same request ID")
                    else:
                        require(body["error"] == "payment_timeout", "must report timeout")
                    latencies.append(latency)
                    errors += status >= 400
                    note(f"POST /checkout X-Request-ID={request_id} -> HTTP {status} "
                         f"{latency:.3f}ms {raw}")
                    require(all(p.poll() is None for p in processes), "service unexpectedly exited")
                    require(call(checkout_url + "/health")[0] == 200,
                            "Checkout must stay healthy, including after timeouts")
                # Allow late Payment handlers to finish before snapshotting their metrics.
                if delay:
                    time.sleep(delay + 0.1)
                total_requests += count
                total_errors += errors
                summary = {"phase": phase, "requests": count, "errors": errors,
                           "phase_error_rate": errors / count,
                           "cumulative_error_rate": total_errors / total_requests,
                           "mean_latency_ms": round(statistics.mean(latencies), 3)}
                phases.append(summary)
                note("SUMMARY " + json.dumps(summary))
                note("GET /health -> HTTP 200; both original service processes still running")
                for name, url in [("checkout", checkout_url), ("payment", payment_url)]:
                    status, metrics, _, _ = call(url + "/metrics")
                    require(status == 200, "metrics unavailable")
                    (output / f"{phase}-{name}.prom").write_text(metrics)
            # Check correlation against actual server logs, not just HTTP responses.
            for name in ("checkout", "payment"):
                events = [json.loads(line) for line in (output / f"{name}.jsonl").read_text().splitlines()]
                failed = [event for event in events if event.get("request_id") == "failure-01"]
                require(any(e["event"] == "request_started" for e in failed), "missing trace start")
                expected_event = "payment_timeout" if name == "checkout" else "delay_injected"
                require(any(e["event"] == expected_event for e in failed), "missing correlated fault")
            require(phases[2]["cumulative_error_rate"] < phases[1]["cumulative_error_rate"],
                    "cumulative error ratio must decrease")
            require(phases[2]["mean_latency_ms"] < phases[1]["mean_latency_ms"],
                    "latency must recover")
            require(phases[2]["mean_latency_ms"] < 100,
                    "recovered mean latency must be below 100 ms (well below the 300 ms timeout)")
            (output / "summary.json").write_text(json.dumps(phases, indent=2) + "\n")
            note("\nPASS: success, timeout detection, health, shared IDs, metrics, and recovery verified.")
            return phases
    finally:
        for process in processes:
            process.terminate()
        for process in processes:
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        for handle in handles:
            handle.close()
        (output / "terminal.txt").write_text("\n".join(transcript) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "evidence")
    run_demo(parser.parse_args().output)
