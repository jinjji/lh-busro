"""Prepare/install the per-user macOS LaunchAgent for this checkout."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import plistlib
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
PYTHON = Path("/Users/jinji/Coding/.venv/bin/python")
LABEL = "com.lhbusro.checker"
DESTINATION = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
PREPARED = ROOT / "deployment" / f"{LABEL}.plist"


def configuration() -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": [str(PYTHON), "-u", str(ROOT / "runtime.py")],
        "WorkingDirectory": str(ROOT),
        "EnvironmentVariables": {"LH_BUSRO_HEADLESS": "true", "LH_BUSRO_SOURCE": "mac-launchagent"},
        "StartInterval": 900,
        "RunAtLoad": True,
        "ProcessType": "Background",
        "ExitTimeOut": 5,
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": str(ROOT / "logs" / "launchagent-error.log"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "install", "uninstall", "status"))
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 14):
        raise SystemExit("Python 3.14를 사용하세요")
    domain = f"gui/{os.getuid()}"
    service = f"{domain}/{LABEL}"
    if args.action == "status":
        return subprocess.run(["launchctl", "print", service], check=False).returncode
    if args.action in {"prepare", "install"}:
        PREPARED.parent.mkdir(exist_ok=True)
        PREPARED.write_bytes(plistlib.dumps(configuration()))
        print(f"설정 파일: {PREPARED}")
    if args.action == "install":
        if Path(sys.executable) != PYTHON or not PYTHON.exists():
            raise SystemExit(f"공유 인터프리터로 실행하세요: {PYTHON}")
        (ROOT / "logs").mkdir(exist_ok=True, mode=0o700)
        DESTINATION.parent.mkdir(parents=True, exist_ok=True)
        existing = subprocess.run(["launchctl", "print", service], capture_output=True)
        if existing.returncode == 0:
            subprocess.run(["launchctl", "bootout", service], check=True)
        DESTINATION.write_bytes(PREPARED.read_bytes())
        DESTINATION.chmod(0o600)
        subprocess.run(["launchctl", "enable", service], check=True)
        subprocess.run(["launchctl", "bootstrap", domain, str(DESTINATION)], check=True)
        return subprocess.run(["launchctl", "print", service], check=False).returncode
    if args.action == "uninstall":
        existing = subprocess.run(["launchctl", "print", service], capture_output=True)
        if existing.returncode == 0:
            subprocess.run(["launchctl", "bootout", service], check=True)
        DESTINATION.unlink(missing_ok=True)
        print("자동 실행을 중지하고 LaunchAgent를 제거했습니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
