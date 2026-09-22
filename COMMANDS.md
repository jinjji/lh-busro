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

## 필요할 때만 15분 간격으로 백그라운드 확인

```bash
/Users/jinji/Coding/.venv/bin/python manage_schedule.py start
/Users/jinji/Coding/.venv/bin/python manage_schedule.py status
/Users/jinji/Coding/.venv/bin/python manage_schedule.py stop
```

`start`는 터미널과 분리된 Python 스케줄러를 시작합니다. 즉시 한 번 조회하고 시작 시각 기준으로 15분마다 실행합니다. 터미널을 닫아도 실행되지만 재부팅·로그아웃 후에는 자동 재시작하지 않습니다. 조회는 브라우저 창 없이 실행됩니다.

알림 없는 반복 테스트는 `manage_schedule.py start --no-notify`를 사용합니다. 이미 실행 중이라면 두 번째 `start`는 추가 스케줄러를 만들지 않습니다. 옵션을 바꾸려면 먼저 `stop`을 실행하세요.

`stop`은 스케줄러와 해당 스케줄러가 진행 중인 조회를 함께 종료합니다. 직접 실행한 `main.py`의 조회에는 영향을 주지 않습니다. Mac이 잠든 동안 빠진 회차를 몰아서 실행하지 않습니다. GitHub Actions는 수동 실행만 지원합니다.

## 로그와 실패 판정

- `logs/<실행ID>.log`: 시도별 단계, 오류 traceback, 실행 코드 해시, 파일·Python 경로, 실행 출처.
- `logs/<실행ID>/attempt_N.html`, `.png`: 실패한 화면의 진단 자료. 입력값을 제거/가리고 URL 쿼리와 인증정보를 로그에서 지웁니다.
- `logs/<실행ID>/seats.json`: 기존 `LH_BUSRO_SAVE_SEATS_JSON_ON_ALERT=true` 설정에서 알림 대상 좌석이 있을 때만 저장합니다.
- `logs/scheduler-YYYYMMDD.log`: 백그라운드 스케줄러의 시작·종료·회차 기록.

실행 로그·진단·스케줄러 로그는 14일 후 다음 실행에서 정리합니다. 종료 코드 `0`은 정상 조회, 마감 또는 중복 회차 생략입니다. `1`은 최종 조회 실패, 설정 오류, 시간 제한 또는 설정된 웹훅 전송 실패입니다. 로그의 마지막 결과를 함께 확인하세요.

네트워크·화면 전환 오류는 5초 후 새 브라우저에서 한 번만 재시도합니다. 로그인 거부·설정 오류·코드 오류는 즉시 실패합니다. 실패 알림은 최종 실패 때 한 번 보냅니다. 조회 프로세스는 285초에 종료하며 정리 및 알림을 포함해 약 5분 이내로 제한합니다. 알림 전송 실패 때문에 조회를 반복하지 않습니다.

## 검증

```bash
/Users/jinji/Coding/.venv/bin/python -m unittest discover -s tests -v
/Users/jinji/Coding/.venv/bin/python -m pip check
```

테스트는 로컬 HTML 및 가짜 응답으로 실행하며 실제 웹훅은 호출하지 않습니다. 실제 사이트 검증은 위 `--no-notify` 명령으로 별도 실행합니다. 좌석 조회까지만 수행하며 예약 확정 동작은 없습니다.
