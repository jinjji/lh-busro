# LH버스로 실행과 오류 확인

공유 환경 `/Users/jinji/Coding/.venv/bin/python` (Python 3.14)을 사용합니다.
프로젝트 경로는 `/Users/jinji/Coding/Workspace/LH_busro`입니다. 아래 명령은 이 디렉터리에서 실행합니다.

## 수동 실행

```bash
/Users/jinji/Coding/.venv/bin/python main.py
```

디스코드 전송 없이 실제 사이트에서 조회만 하려면:

```bash
LH_BUSRO_HEADLESS=true /Users/jinji/Coding/.venv/bin/python main.py --no-notify
```

`.env`는 실행 위치와 무관하게 프로젝트 디렉터리에서 읽습니다. 기존 환경변수 이름과 기본 조회 조건은 유지됩니다. 로그인 아이디·비밀번호는 필수이며, 웹훅 미설정 시 알림만 생략합니다. `--no-notify`는 실패 알림도 보내지 않습니다.

## 24시간 자동 확인 (Mac 로그인 중)

```bash
/Users/jinji/Coding/.venv/bin/python manage_schedule.py prepare
/Users/jinji/Coding/.venv/bin/python manage_schedule.py install
/Users/jinji/Coding/.venv/bin/python manage_schedule.py status
```

`prepare`는 검토용 plist를 `deployment/`에 만듭니다. `install`은 사용자 `~/Library/LaunchAgents/com.lhbusro.checker.plist`를 설치하고 즉시 1회 실행합니다. 이후 900초 간격 및 다음 로그인 시 실행하며 브라우저 창을 띄우지 않습니다. 실행에 사용할 코드는 이 체크아웃으로 고정됩니다.

Mac이 꺼져 있거나 로그아웃한 동안은 실행되지 않습니다. 잠자는 동안의 모든 회차를 몰아서 실행하지 않으며, 깨어난 뒤 스케줄이 재개됩니다. 중복 회차는 파일 잠금으로 건너뜁니다. 컴퓨터의 잠자기 설정은 바꾸지 않습니다.

자동 실행 중지:

```bash
/Users/jinji/Coding/.venv/bin/python manage_schedule.py uninstall
```

GitHub Actions는 수동 실행만 남겨 놓았습니다. **이 변경이 원격 저장소의 기본 브랜치에 반영되기 전에는 기존 GitHub 예약 실행이 계속될 수 있습니다.** 로컬 잠금은 다른 컴퓨터/Actions와 공유되지 않습니다.

## 로그와 실패 판정

- `logs/<실행ID>.log`: 시도별 단계, 오류 traceback, 실행 코드 해시, 파일·Python 경로, 실행 출처.
- `logs/<실행ID>/attempt_N.html`, `.png`: 실패한 화면의 진단 자료. 입력값을 제거/가리고 URL 쿼리와 인증정보를 로그에서 지웁니다.
- `logs/<실행ID>/seats.json`: 기존 `LH_BUSRO_SAVE_SEATS_JSON_ON_ALERT=true` 설정에서 알림 대상 좌석이 있을 때만 저장합니다.
- `logs/launchagent-error.log`: Python 실행 전후의 LaunchAgent 시작 오류.

실행 로그·진단은 14일 후 다음 실행에서 정리합니다. 종료 코드 `0`은 정상 조회, 마감 또는 중복 회차 생략입니다. `1`은 최종 조회 실패, 설정 오류, 시간 제한 또는 설정된 웹훅 전송 실패입니다. 로그의 마지막 결과를 함께 확인하세요.

네트워크·화면 전환 오류는 5초 후 새 브라우저에서 한 번만 재시도합니다. 로그인 거부·설정 오류·코드 오류는 즉시 실패합니다. 실패 알림은 최종 실패 때 한 번 보냅니다. 조회 프로세스는 285초에 종료하며 정리 및 알림을 포함해 약 5분 이내로 제한합니다. 알림 전송 실패 때문에 조회를 반복하지 않습니다.

## 검증

```bash
/Users/jinji/Coding/.venv/bin/python -m unittest discover -s tests -v
/Users/jinji/Coding/.venv/bin/python -m pip check
```

테스트는 로컬 HTML 및 가짜 응답으로 실행하며 실제 웹훅은 호출하지 않습니다. 실제 사이트 검증은 위 `--no-notify` 명령으로 별도 실행합니다. 좌석 조회까지만 수행하며 예약 확정 동작은 없습니다.
