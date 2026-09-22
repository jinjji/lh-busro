import json
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import dashboard


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "dashboard.html").write_text("token=__DASHBOARD_TOKEN__", encoding="utf-8")
        self.app = dashboard.Dashboard(self.root)

    def test_status_and_recent_logs(self):
        with patch.object(dashboard.manage_schedule, "request", return_value={
            "status": "running", "checking": False, "next_run_at": 12345,
            "no_notify": False}):
            status = self.app.status()
        self.assertEqual(status["scheduler"], "running")
        self.assertEqual(status["next_run_at"], 12345)
        self.assertTrue(status["notifications"])
        (self.app.logs / "20260923_070000_000001.log").write_text("정상 종료: 대상 배차 마감")
        (self.app.logs / "20260923_070001_000001.log").write_text("최종 실패")
        (self.app.logs / "scheduler-20260923.log").write_text("private")
        history = self.app.history()
        self.assertEqual([item["result"] for item in history], ["실패", "배차 마감"])
        self.assertEqual(len(history), 2)
        with self.assertRaises(ValueError):
            self.app.read_log("../.scheduler.json")

    def test_unavailable_is_not_reported_as_stopped(self):
        dashboard.private_json(self.app.logs / ".scheduler.json", {"token": "stale"})
        with patch.object(dashboard.manage_schedule, "request", return_value=None):
            self.assertEqual(self.app.status()["scheduler"], "unavailable")

    def test_one_shot_starts_and_stops_without_live_site(self):
        (self.root / "main.py").write_text(
            "from pathlib import Path\nimport time\nPath('started').write_text('yes')\ntime.sleep(30)\n",
            encoding="utf-8")
        self.assertIn("시작", self.app.action("check"))
        try:
            deadline = time.monotonic() + 3
            while not (self.root / "started").exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue((self.root / "started").exists())
            self.assertTrue(self.app.status()["manual_checking"])
            self.assertIn("중단", self.app.action("stop"))
            self.assertFalse(self.app.status()["manual_checking"])
        finally:
            if self.app.manual is not None and self.app.manual.poll() is None:
                self.app.action("stop")

    def test_http_requires_token_and_controls(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.handler_for(self.app))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"
        with self.assertRaises(HTTPError) as denied:
            urlopen(base + "/api/status")
        self.assertEqual(denied.exception.code, 403)
        denied.exception.close()
        headers = {"X-Dashboard-Token": self.app.token}
        with urlopen(Request(base + "/api/status", headers=headers)) as response:
            self.assertEqual(json.load(response)["scheduler"], "stopped")
        with patch.object(self.app, "action", return_value="시작했습니다") as action:
            request = Request(base + "/api/start", headers=headers, method="POST", data=b"")
            with urlopen(request) as response:
                self.assertEqual(json.load(response)["message"], "시작했습니다")
            action.assert_called_once_with("start")
        request = Request(base + "/api/start", headers={**headers, "Origin": "https://example.com"},
                          method="POST", data=b"")
        with self.assertRaises(HTTPError) as denied:
            urlopen(request)
        self.assertEqual(denied.exception.code, 403)
        denied.exception.close()


if __name__ == "__main__":
    unittest.main()
