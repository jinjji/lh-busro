"""Opt-in Python background scheduler for one-shot bus checks."""
from __future__ import annotations

import argparse
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import secrets
import selectors
import signal
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
INTERVAL_SECONDS = 900


def paths(root: Path):
    logs = root / "logs"
    return logs, logs / ".scheduler.lock", logs / ".scheduler.sock", logs / ".scheduler.json"


def next_due(previous: float, now: float, interval: float) -> float:
    """Return the next wall-clock slot without replaying missed intervals."""
    return previous + (int(max(0, now - previous) // interval) + 1) * interval


def read_state(path: Path) -> dict | None:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        return state if isinstance(state, dict) and isinstance(state.get("token"), str) else None
    except (OSError, ValueError):
        return None


def write_state(path: Path, state: dict) -> None:
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        with open(temporary, "w", encoding="utf-8", opener=lambda p, f: os.open(p, f, 0o600)) as output:
            json.dump(state, output, ensure_ascii=False)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def request(root: Path, action: str) -> dict | None:
    _, _, socket_path, state_path = paths(root)
    state = read_state(state_path)
    if state is None:
        return None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(2)
            connection.connect(str(socket_path))
            connection.sendall((json.dumps({"action": action, "token": state["token"]}) + "\n").encode())
            response = connection.makefile("r", encoding="utf-8").readline()
        result = json.loads(response)
        return result if isinstance(result, dict) else None
    except (OSError, ValueError):
        return None


def print_status(result: dict | None) -> None:
    if result is None:
        print("스케줄러 중지됨")
    else:
        print(f"스케줄러 실행 중: PID {result['pid']}, "
              f"조회 중={'예' if result['checking'] else '아니요'}, "
              f"다음 예정={datetime.fromtimestamp(result['next_run_at']).isoformat()}, "
              f"알림={'끔' if result['no_notify'] else '켬'}")


def start(root: Path, *, no_notify: bool = False, interval: float = INTERVAL_SECONDS) -> int:
    if interval <= 0:
        raise ValueError("조회 간격은 양수여야 합니다")
    running = request(root, "status")
    if running is not None:
        print_status(running)
        print("이미 실행 중입니다. 설정 변경은 stop 후 start 하세요.")
        return 0
    logs, _, _, _ = paths(root)
    logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent, child = socket.socketpair()
    command = [sys.executable, "-u", str(Path(__file__).resolve()), "_serve",
               "--root", str(root), "--interval", str(interval), "--ready-fd", str(child.fileno())]
    if no_notify:
        command.append("--no-notify")
    day = datetime.now().strftime("%Y%m%d")
    with (logs / f"scheduler-{day}.log").open("a", encoding="utf-8") as output:
        process = subprocess.Popen(command, cwd=root, stdin=subprocess.DEVNULL,
                                   stdout=output, stderr=subprocess.STDOUT,
                                   start_new_session=True, pass_fds=(child.fileno(),),
                                   env={**os.environ, "LH_BUSRO_HEADLESS": "true"})
    child.close()
    try:
        parent.settimeout(8)
        reply = json.loads(parent.makefile("r", encoding="utf-8").readline())
    except (OSError, ValueError) as error:
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        print(f"스케줄러 시작 확인 실패: {error}; PID {process.pid}", file=sys.stderr)
        return 1
    finally:
        parent.close()
    process.wait(timeout=2)
    if reply.get("status") == "already":
        print("이미 실행 중입니다. 설정 변경은 stop 후 start 하세요.")
        return 0
    if reply.get("status") != "ready":
        print(f"스케줄러 시작 실패: {reply.get('error', '알 수 없는 오류')}", file=sys.stderr)
        return 1
    print_status(request(root, "status"))
    return 0


def stop(root: Path) -> int:
    response = request(root, "stop")
    if response is None:
        print("스케줄러가 실행 중이지 않습니다.")
        return 0
    if response.get("status") != "stopping":
        print(f"스케줄러 종료 요청 실패: {response}", file=sys.stderr)
        return 1
    _, _, socket_path, state_path = paths(root)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if not socket_path.exists() and not state_path.exists():
            print("스케줄러와 해당 조회를 종료했습니다.")
            return 0
        time.sleep(0.1)
    print("종료 요청 후에도 스케줄러가 실행 중입니다. status로 확인하세요.", file=sys.stderr)
    return 1


def terminate_run(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def serve(root: Path, *, ready_fd: int, no_notify: bool, interval: float) -> int:
    os.umask(0o077)
    logs, lock_path, socket_path, state_path = paths(root)
    logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    with os.fdopen(ready_fd, "w", encoding="utf-8") as ready:
        with lock_path.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                ready.write('{"status":"already"}\n')
                ready.flush()
                return 0
            # Only the lock owner may remove stale control files.
            socket_path.unlink(missing_ok=True)
            state_path.unlink(missing_ok=True)
            token = secrets.token_hex(24)
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                listener.bind(str(socket_path))
                os.chmod(socket_path, 0o600)
                listener.listen(5)
                listener.setblocking(False)
                selector = selectors.DefaultSelector()
                selector.register(listener, selectors.EVENT_READ)
                log_day = datetime.now().strftime("%Y%m%d")
                started = time.time()
                next_run = started
                current: subprocess.Popen | None = None
                stopping = False
                state = {"token": token, "pid": os.getpid(), "started_at": started,
                         "next_run_at": next_run, "no_notify": no_notify,
                         "checking": False, "last_exit_code": None}
                write_state(state_path, state)
                ready.write('{"status":"ready"}\n')
                ready.flush()
                print(f"스케줄러 시작: PID {os.getpid()}, 간격 {interval:g}초", flush=True)

                def signal_stop(_signum, _frame):
                    nonlocal stopping
                    stopping = True

                signal.signal(signal.SIGTERM, signal_stop)
                signal.signal(signal.SIGINT, signal_stop)
                try:
                    while not stopping:
                        now = time.time()
                        today = datetime.fromtimestamp(now).strftime("%Y%m%d")
                        if today != log_day:
                            old_output = sys.stdout
                            rotated = (logs / f"scheduler-{today}.log").open("a", encoding="utf-8")
                            sys.stdout = rotated
                            sys.stderr = rotated
                            old_output.close()
                            log_day = today
                        if current is not None and current.poll() is not None:
                            state["last_exit_code"] = current.returncode
                            state["checking"] = False
                            print(f"조회 종료: 코드 {current.returncode}", flush=True)
                            current = None
                            write_state(state_path, state)
                        if now >= next_run and current is None:
                            # One check after sleep; skip all missed slots.
                            next_run = next_due(next_run, now, interval)
                            state["next_run_at"] = next_run
                            command = [sys.executable, "-u", str(root / "runtime.py")]
                            if no_notify:
                                command.append("--no-notify")
                            current = subprocess.Popen(command, cwd=root, stdin=subprocess.DEVNULL,
                                                       stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
                                                       start_new_session=True,
                                                       env={**os.environ, "LH_BUSRO_SOURCE": "python-scheduler",
                                                            "LH_BUSRO_HEADLESS": "true"})
                            state["checking"] = True
                            print(f"조회 시작: PID {current.pid}; 다음 예정 "
                                  f"{datetime.fromtimestamp(next_run).isoformat()}", flush=True)
                            write_state(state_path, state)
                        wait = min(0.25, max(0, next_run - time.time()))
                        for key, _ in selector.select(wait):
                            with key.fileobj.accept()[0] as connection:
                                connection.settimeout(1)
                                try:
                                    raw = connection.makefile("r", encoding="utf-8").readline()
                                    message = json.loads(raw)
                                    if message.get("token") != token:
                                        reply = {"status": "unauthorized"}
                                    elif message.get("action") == "status":
                                        reply = {"status": "running", **state}
                                    elif message.get("action") == "stop":
                                        stopping = True
                                        reply = {"status": "stopping"}
                                    else:
                                        reply = {"status": "invalid"}
                                    connection.sendall((json.dumps(reply) + "\n").encode())
                                except (OSError, ValueError):
                                    pass
                finally:
                    if current is not None and current.poll() is None:
                        terminate_run(current)
                    print("스케줄러 종료", flush=True)
                return 0
            finally:
                listener.close()
                socket_path.unlink(missing_ok=True)
                state_path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "status", "stop", "_serve"))
    parser.add_argument("--no-notify", action="store_true", help="디스코드 알림 없이 반복 조회")
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    parser.add_argument("--interval", type=float, default=INTERVAL_SECONDS, help=argparse.SUPPRESS)
    parser.add_argument("--ready-fd", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 14):
        parser.error("Python 3.14 공유 환경을 사용하세요")
    if args.action == "start":
        return start(args.root, no_notify=args.no_notify, interval=args.interval)
    if args.action == "status":
        print_status(request(args.root, "status"))
        return 0
    if args.action == "stop":
        return stop(args.root)
    if args.ready_fd is None:
        parser.error("내부 실행에는 --ready-fd가 필요합니다")
    # Reap the short-lived launcher while the grandchild owns the scheduler.
    if os.fork():
        os._exit(0)
    os.setsid()
    return serve(args.root, ready_fd=args.ready_fd, no_notify=args.no_notify, interval=args.interval)


if __name__ == "__main__":
    sys.exit(main())
