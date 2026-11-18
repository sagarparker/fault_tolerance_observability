"""Mock payment processor with a local, removable fault injection delay."""

import argparse
import math
import time
from pathlib import Path

from services.common import Handler, log, serve


class PaymentHandler(Handler):
    def process(self, data):
        delay_file = self.server.delay_file
        delay = float(Path(delay_file).read_text().strip()) if delay_file else 0.0
        if not math.isfinite(delay) or delay < 0:
            raise ValueError("delay must be a finite, nonnegative number")
        if delay:
            log("payment", "delay_injected", request_id=self.request_id, delay_seconds=delay)
            time.sleep(delay)
        return 200, {"status": "paid", "order_id": data["order_id"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--delay-file", default=None,
                        help="Optional file containing delay seconds, reread for every request")
    args = parser.parse_args()
    serve("payment", args.port, PaymentHandler, "/pay", delay_file=args.delay_file)
