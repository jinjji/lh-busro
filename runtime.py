"""Single-run lock, bounded worker process and 14-day log retention (macOS/Linux)."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time


@contextmanager
def run_lock(path: Path):
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def prune_logs(directory: Path, *, now: float | None = None) -> None:
    cutoff = (time.time() if now is None else now) - 14 * 86400
    for path in directory.iterdir():
        # Only remove recognized application artifacts, never arbitrary files.
        is_run = bool(re.fullmatch(r"\d{8}_\d{6}(?:_\d+)?(?:\.log)?", path.name))
        is_scheduler = bool(re.fullmatch(r"scheduler-\d{8}\.log", path.name))
        if path.is_symlink() or not (is_run or is_scheduler):
            continue
        if path.stat().st_mtime < cutoff:
            if path.is_dir():
                shutil.rmtree(path)
            elif path.suffix == ".log":
                path.unlink()


def stop_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    # Chromium descendants may outlive the Python worker; always clean the group.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def supervise(root: Path, run_id: str, *, no_notify: bool = False,
              worker_timeout: float = 285) -> int:
    os.umask(0o077)
    logs = root / "logs"
    logs.mkdir(exist_ok=True, mode=0o700)
    with run_lock(logs / ".run.lock") as acquired:
        if not acquired:
            print("이미 실행 중인 회차가 있어 이번 실행을 생략합니다.", flush=True)
            return 0
        prune_logs(logs)
        log_path = logs / f"{run_id}.log"
        command = [sys.executable, "-u", str(root / "main.py"), "--worker"]
        if no_notify:
            command.append("--no-notify")
        env = {**os.environ, "LH_BUSRO_RUN_ID": run_id}
        with log_path.open("w", encoding="utf-8") as output:
            process = subprocess.Popen(command, cwd=root, env=env, stdout=output,
                                       stderr=subprocess.STDOUT, start_new_session=True)
            def interrupted(signum, frame):
                stop_group(process)
                raise SystemExit(1)
            previous = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGTERM, signal.SIGINT)}
            try:
                try:
                    code = process.wait(timeout=worker_timeout)
                except subprocess.TimeoutExpired:
                    stop_group(process)
                    output.write("실행 시간 제한 초과: 브라우저와 조회 프로세스를 종료했습니다.\n")
                    output.flush()
                    from main import notify_failure
                    notify_failure("전체 실행 시간 제한", TimeoutError("최대 실행 시간 초과"), notify=not no_notify)
                    code = 1
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
        print(log_path.read_text(encoding="utf-8"), end="", flush=True)
        print(f"실행 로그: {log_path}; 종료 코드={code}", flush=True)
        return 0 if code == 0 else 1


if __name__ == "__main__":
    from datetime import datetime
    if sys.version_info[:2] != (3, 14):
        raise SystemExit("Python 3.14 공유 환경을 사용하세요")
    sys.exit(supervise(Path(__file__).resolve().parent,
                       datetime.now().strftime("%Y%m%d_%H%M%S_%f"),
                       no_notify="--no-notify" in sys.argv))
