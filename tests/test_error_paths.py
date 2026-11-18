"""Regression tests for failure attribution across the service boundary."""

import tempfile
import threading
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from scripts.demo import call
from services.checkout import CheckoutHandler
from services.common import Metrics
from services.payment import PaymentHandler


@contextmanager
def running(handler, service, path, **config):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.service = service
    server.business_path = path
    server.metrics = Metrics(service)
    for key, value in config.items():
        setattr(server, key, value)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class DroppedConnectionHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.close_connection = True  # Close without sending HTTP headers.


class ErrorPathTest(unittest.TestCase):
    def test_dropped_payment_connection_returns_502_and_attributes_failure(self):
        with patch("services.common.log") as logs, patch("services.checkout.log") as checkout_logs:
            with running(DroppedConnectionHandler, "payment", "/pay") as payment:
                with running(CheckoutHandler, "checkout", "/checkout",
                             payment_url=payment, payment_timeout=0.3) as checkout:
                    status, body, _, headers = call(checkout + "/checkout",
                                                  {"order_id": "drop-test"}, "drop-01")
                    self.assertEqual(status, 502)
                    self.assertIn('"error": "payment_unavailable"', body)
                    self.assertEqual(headers["X-Request-ID"], "drop-01")
                    self.assertEqual(call(checkout + "/health")[0], 200)
            self.assertTrue(any(c.args[1] == "payment_unavailable"
                                for c in checkout_logs.call_args_list))
            self.assertFalse(any(c.args[1] == "client_disconnected" for c in logs.call_args_list))
            completed = [c.kwargs for c in logs.call_args_list if c.args[1] == "request_completed"]
            self.assertEqual(completed[0]["status"], 502)

    def test_invalid_delay_configuration_is_a_server_error(self):
        with tempfile.TemporaryDirectory() as directory, patch("services.common.log") as logs:
            delay = Path(directory) / "delay.txt"
            with running(PaymentHandler, "payment", "/pay", delay_file=str(delay)) as payment:
                for value in ("abc", "-1", "nan"):
                    with self.subTest(delay=value):
                        delay.write_text(value)
                        status, body, _, _ = call(payment + "/pay", {"order_id": "config-test"})
                        self.assertEqual(status, 500)
                        self.assertIn('"error": "internal_error"', body)
            self.assertEqual(sum(c.args[1] == "internal_error" for c in logs.call_args_list), 3)

    def test_invalid_order_still_returns_client_error(self):
        with patch("services.common.log"):
            with running(PaymentHandler, "payment", "/pay", delay_file=None) as payment:
                status, body, _, _ = call(payment + "/pay", {"order_id": ""})
                self.assertEqual(status, 400)
                self.assertIn('"error": "invalid_request"', body)
