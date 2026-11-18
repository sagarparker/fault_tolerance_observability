"""Exercise real HTTP services and inspect independently recorded metrics."""

import tempfile
import unittest
from pathlib import Path

from scripts.demo import run_demo


class CheckoutPaymentTest(unittest.TestCase):
    def test_success_timeout_and_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            phases = run_demo(output)
            self.assertEqual([p["errors"] for p in phases], [0, 3, 0])
            metrics = (output / "recovery-checkout.prom").read_text()
            self.assertIn('app_requests_total{service="checkout",outcome="success"} 9', metrics)
            self.assertIn('app_requests_total{service="checkout",outcome="failure"} 3', metrics)
            self.assertIn('app_request_duration_seconds_count{service="checkout"} 12', metrics)
            payment = (output / "recovery-payment.prom").read_text()
            self.assertIn('app_request_duration_seconds_count{service="payment"} 12', payment)


if __name__ == "__main__":
    unittest.main()
