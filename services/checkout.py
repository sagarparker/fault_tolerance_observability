"""Checkout calls Payment with a bounded timeout and propagates request IDs."""

import argparse
import json
import math
import socket
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from services.common import Handler, log, serve


class CheckoutHandler(Handler):
    def process(self, data):
        request = Request(self.server.payment_url + "/pay", method="POST",
                          data=json.dumps(data).encode(),
                          headers={"Content-Type": "application/json",
                                   "X-Request-ID": self.request_id})
        started = time.monotonic()
        log("checkout", "payment_call_started", request_id=self.request_id,
            timeout_seconds=self.server.payment_timeout)
        try:
            with urlopen(request, timeout=self.server.payment_timeout) as response:
                payment = json.load(response)
            log("checkout", "payment_call_completed", request_id=self.request_id,
                duration_ms=round((time.monotonic() - started) * 1000, 3))
            return 200, {"status": "confirmed", "order_id": data["order_id"], "payment": payment}
        except (TimeoutError, socket.timeout):
            return self.timeout_result()
        except HTTPError as exc:
            log("checkout", "payment_error", request_id=self.request_id, error=str(exc))
            return 502, {"error": "payment_error"}
        except URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                return self.timeout_result()
            log("checkout", "payment_unavailable", request_id=self.request_id, error=str(exc))
            return 502, {"error": "payment_unavailable"}
        except ValueError as exc:
            log("checkout", "payment_error", request_id=self.request_id, error=str(exc))
            return 502, {"error": "payment_error"}

    def timeout_result(self):
        log("checkout", "payment_timeout", request_id=self.request_id,
            timeout_seconds=self.server.payment_timeout)
        return 504, {"error": "payment_timeout"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--payment-url", default="http://127.0.0.1:8001")
    parser.add_argument("--payment-timeout", type=float, default=0.3)
    args = parser.parse_args()
    if not math.isfinite(args.payment_timeout) or args.payment_timeout <= 0:
        parser.error("--payment-timeout must be positive")
    serve("checkout", args.port, CheckoutHandler, "/checkout",
          payment_url=args.payment_url.rstrip("/"), payment_timeout=args.payment_timeout)
