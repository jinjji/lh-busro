"""Small local dashboard for the existing bus checker."""
from __future__ import annotations

from datetime import datetime
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
from urllib.request import Request, urlopen
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit

import manage_schedule


ROOT = Path(__file__).resolve().parent
PYTHON = Path("/Users/jinji/Coding/.venv/bin/python")
RUN_LOG = re.compile(r"\d{8}_\d{6}_\d+\.log\Z")


def private_json(path: Path, value: dict) -> None:
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with open(temporary, "w", encoding="utf-8", opener=lambda p, f: os.open(p, f, 0o600)) as output:
            json.dump(value, output)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def read_dashboard_state(root: Path) -> dict | None:
    try:
        value = json.loads((root / "logs/.dashboard.json").read_text(encoding="utf-8"))
        if isinstance(value.get("port"), int) and isinstance(value.get("token"), str):
            return value
    except (OSError, ValueError, AttributeError):
        pass
    return None


def dashboard_alive(state: dict | None) -> bool:
    if not state:
        return False
    try:
        url = f"http://127.0.0.1:{state['port']}/api/ping"
        with urlopen(Request(url, headers={"X-Dashboard-Token": state["token"]}), timeout=0.5) as response:
            return response.status == 200
    except (OSError, URLError, ValueError):
        return False


class Dashboard:
    def __init__(self, root: Path):
        self.root = root
        self.logs = root / "logs"
        self.logs.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.manual: subprocess.Popen | None = None
        self.lock = threading.Lock()
        self.token = secrets.token_urlsafe(32)

    def scheduler(self) -> tuple[str, dict | None]:
        state = manage_schedule.request(self.root, "status")
        if state is not None:
            return "running", state
        if manage_schedule.read_state(self.logs / ".scheduler.json") is not None:
            return "unavailable", None
        return "stopped", None

    def check_busy(self) -> bool:
        path = self.logs / ".run.lock"
        with path.open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)
                return False

    def status(self) -> dict:
        scheduler, state = self.scheduler()
        with self.lock:
            manual = self.manual is not None and self.manual.poll() is None
        busy = self.check_busy() or manual or bool(state and state.get("checking"))
        return {"scheduler": scheduler, "checking": busy,
                "manual_checking": manual,
                "next_run_at": state.get("next_run_at") if state else None,
                "notifications": None if state is None else not state.get("no_notify", False)}

    def action(self, name: str) -> str:
        with self.lock:
            if name == "start":
                scheduler, _ = self.scheduler()
                if scheduler == "unavailable":
                    raise RuntimeError("스케줄러 상태를 확인할 수 없습니다")
                if scheduler == "running":
                    return "이미 15분 반복 실행 중입니다"
                if self.check_busy():
                    raise RuntimeError("조회가 끝난 뒤 다시 시도하세요")
                command = [str(PYTHON), str(self.root / "manage_schedule.py"), "start"]
                result = subprocess.run(command, cwd=self.root, capture_output=True,
                                        text=True, timeout=15, check=False)
                if result.returncode:
                    raise RuntimeError("시작 실패: " + (result.stderr.strip() or "스케줄러 로그를 확인하세요"))
                return "15분 반복 실행을 시작했습니다"
            if name == "check":
                if self.check_busy() or (self.manual is not None and self.manual.poll() is None):
                    raise RuntimeError("이미 조회 중입니다")
                env = {**os.environ, "LH_BUSRO_HEADLESS": "true", "LH_BUSRO_SOURCE": "dashboard-manual"}
                self.manual = subprocess.Popen(
                    [str(PYTHON), str(self.root / "main.py")], cwd=self.root,
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, start_new_session=True, env=env)
                return "1회 조회를 시작했습니다"
            if name == "stop":
                if self.manual is not None and self.manual.poll() is None:
                    try:
                        os.killpg(self.manual.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    try:
                        self.manual.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        try:
                            os.killpg(self.manual.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        self.manual.wait()
                scheduler, _ = self.scheduler()
                if scheduler == "unavailable":
                    raise RuntimeError("1회 조회를 중단했지만 스케줄러 상태를 확인할 수 없습니다")
                if scheduler == "running":
                    result = subprocess.run(
                        [str(PYTHON), str(self.root / "manage_schedule.py"), "stop"],
                        cwd=self.root, capture_output=True, text=True, timeout=15, check=False)
                    if result.returncode:
                        raise RuntimeError("스케줄러 중단 실패: " + (result.stderr.strip() or "상태를 확인하세요"))
                return "실행을 중단했습니다"
        raise ValueError("알 수 없는 동작입니다")

    def history(self) -> list[dict]:
        entries = []
        for path in sorted(self.logs.iterdir(), reverse=True):
            if not RUN_LOG.fullmatch(path.name) or not path.is_file() or path.is_symlink():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")[-100_000:]
            if "정상 종료: 대상 배차 마감" in text:
                result = "배차 마감"
            elif "정상 종료: 조회 완료" in text:
                result = "조회 완료"
            elif "최종 실패" in text or "실행 시간 제한 초과" in text:
                result = "실패"
            else:
                result = "진행 중" if time.time() - path.stat().st_mtime < 300 else "완료 기록 없음"
            entries.append({"name": path.name, "result": result})
            if len(entries) == 20:
                break
        return entries

    def read_log(self, name: str) -> str:
        if not RUN_LOG.fullmatch(name):
            raise ValueError("잘못된 로그 이름입니다")
        path = self.logs / name
        if not path.is_file() or path.is_symlink():
            raise FileNotFoundError(name)
        return path.read_text(encoding="utf-8", errors="replace")[-100_000:]


def handler_for(dashboard: Dashboard):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format_string, *args):
            pass

        def send(self, code: int, data: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def json(self, code: int, value: dict | list) -> None:
            self.send(code, json.dumps(value, ensure_ascii=False).encode("utf-8"),
                      "application/json; charset=utf-8")

        def authorized(self) -> bool:
            return secrets.compare_digest(self.headers.get("X-Dashboard-Token", ""), dashboard.token)

        def valid_host(self) -> bool:
            host = self.headers.get("Host", "")
            return host in {f"127.0.0.1:{self.server.server_port}",
                            f"localhost:{self.server.server_port}"}

        def do_GET(self):
            if not self.valid_host():
                return self.json(403, {"error": "접근할 수 없습니다"})
            url = urlsplit(self.path)
            if url.path == "/":
                if parse_qs(url.query).get("token") != [dashboard.token]:
                    return self.json(403, {"error": "접근할 수 없습니다"})
                template = (dashboard.root / "dashboard.html").read_text(encoding="utf-8")
                page = template.replace("__DASHBOARD_TOKEN__", dashboard.token)
                return self.send(200, page.encode("utf-8"), "text/html; charset=utf-8")
            if not self.authorized():
                return self.json(403, {"error": "접근할 수 없습니다"})
            if url.path == "/api/ping":
                return self.json(200, {"ok": True})
            if url.path == "/api/status":
                return self.json(200, dashboard.status())
            if url.path == "/api/logs":
                return self.json(200, dashboard.history())
            if url.path == "/api/log":
                try:
                    return self.json(200, {"text": dashboard.read_log(parse_qs(url.query).get("name", [""])[0])})
                except (ValueError, FileNotFoundError):
                    return self.json(404, {"error": "로그를 찾을 수 없습니다"})
            return self.json(404, {"error": "페이지를 찾을 수 없습니다"})

        def do_POST(self):
            if not self.valid_host() or not self.authorized():
                return self.json(403, {"error": "접근할 수 없습니다"})
            if self.headers.get("Origin") not in (None, f"http://127.0.0.1:{self.server.server_port}"):
                return self.json(403, {"error": "접근할 수 없습니다"})
            name = urlsplit(self.path).path.removeprefix("/api/")
            if name not in {"start", "stop", "check"}:
                return self.json(404, {"error": "알 수 없는 동작입니다"})
            try:
                return self.json(200, {"message": dashboard.action(name)})
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
                return self.json(409, {"error": str(error)})

    return Handler


def serve(root: Path = ROOT) -> None:
    dashboard = Dashboard(root)
    with (dashboard.logs / ".dashboard.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(dashboard))
        state_path = dashboard.logs / ".dashboard.json"
        private_json(state_path, {"pid": os.getpid(), "port": server.server_port,
                                  "token": dashboard.token})
        try:
            server.serve_forever()
        finally:
            server.server_close()
            state_path.unlink(missing_ok=True)


def open_dashboard(root: Path = ROOT) -> int:
    logs = root / "logs"
    logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    state = read_dashboard_state(root)
    if not dashboard_alive(state):
        with (logs / "dashboard.log").open("a", encoding="utf-8") as output:
            subprocess.Popen([str(PYTHON), str(Path(__file__).resolve()), "serve"],
                             cwd=root, stdin=subprocess.DEVNULL, stdout=output,
                             stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = read_dashboard_state(root)
            if dashboard_alive(state):
                break
            time.sleep(0.1)
        else:
            print("대시보드를 시작하지 못했습니다. logs/dashboard.log를 확인하세요.", file=sys.stderr)
            return 1
    url = f"http://127.0.0.1:{state['port']}/?token={state['token']}"
    subprocess.run(["open", url], check=True)
    return 0


if __name__ == "__main__":
    if sys.version_info[:2] != (3, 14) or Path(sys.executable) != PYTHON:
        raise SystemExit(f"공유 Python 3.14를 사용하세요: {PYTHON}")
    if sys.argv[1:] == ["serve"]:
        serve()
    elif not sys.argv[1:] or sys.argv[1:] == ["open"]:
        raise SystemExit(open_dashboard())
    else:
        raise SystemExit("사용법: dashboard.py [open|serve]")
