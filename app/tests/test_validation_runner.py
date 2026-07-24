from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from core import validation

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "agenelf_validation_runner", ROOT / "scripts" / "validation_runner.py"
)
validation_runner = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = validation_runner
SPEC.loader.exec_module(validation_runner)


class Handler(BaseHTTPRequestHandler):
    response_status = 200
    response_body = b'{"status":"ok","ready":true}'

    def do_GET(self):  # noqa: N802
        self.send_response(self.__class__.response_status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(self.__class__.response_body)

    def log_message(self, format, *args):
        return None


class ValidationRunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        port = self.server.server_address[1]
        self.config = self.root / "validation.yaml"
        self.config.write_text(
            f"""checks:
  health:
    type: http
    url: http://127.0.0.1:{port}/health
    expected_status: [200]
    contains: [ready]
    json_equals:
      status: ok
    timeout_seconds: 2
  bad-json:
    type: http
    url: http://127.0.0.1:{port}/health
    expected_status: [200]
    json_equals:
      status: broken
suites:
  smoke:
    checks: [health]
""",
            encoding="utf-8",
        )
        self.runner = validation_runner.ValidationRunner(
            root=self.root,
            validation_file=self.config,
        )

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def test_check_and_suite_execute_with_trusted_evidence(self):
        request = validation.submit_validation(
            "run_check", "health", "health", root=self.root
        )
        self.assertEqual(self.runner.run_once().get("succeeded"), 1)
        state = validation.get_validation(request["id"], root=self.root)
        self.assertEqual(state["status"], "succeeded")
        result = state["result"]
        self.assertEqual(result["passed"], 1)
        self.assertTrue(result["checks"][0]["passed"])
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("127.0.0.1", serialized)
        self.assertNotIn("/health", serialized)

        suite = validation.submit_validation(
            "run_suite", "smoke", "smoke", root=self.root
        )
        self.assertEqual(self.runner.run_once().get("succeeded"), 1)
        self.assertEqual(
            validation.get_validation(suite["id"], root=self.root)["status"],
            "succeeded",
        )

    def test_failed_assertion_produces_failed_result_not_exception(self):
        request = validation.submit_validation(
            "run_check", "bad-json", "bad", root=self.root
        )
        self.assertEqual(self.runner.run_once().get("failed"), 1)
        result = validation.get_validation(request["id"], root=self.root)["result"]
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["checks"][0]["passed"])

    def test_tampered_request_is_rejected_before_network(self):
        request = validation.submit_validation(
            "run_check", "health", "health", root=self.root
        )
        path = self.root / "data" / "validation-requests" / f"{request['id']}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["target"] = "bad-json"
        path.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(self.runner.run_once().get("failed"), 1)
        result = validation.get_validation(request["id"], root=self.root)["result"]
        self.assertIn("指纹", result["reason"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
