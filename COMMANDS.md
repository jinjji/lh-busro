# LH버스로 실행과 오류 확인

공유 환경 `/Users/jinji/Coding/.venv/bin/python` (Python 3.14)을 사용합니다.
먼저 프로젝트 디렉터리로 이동한 뒤 아래 명령을 실행합니다.

```bash
cd /Users/jinji/Coding/Workspace/LH_busro
```

## `.env` 설정

프로젝트 루트의 `.env`에 `BUS_USERNAME`, `BUS_PASSWORD`를 설정해야 조회할 수 있습니다. `DISCORD_WEBHOOK_URL`은 디스코드 알림을 받을 때 설정합니다. `.env`는 Git에 포함되지 않으며 실행 위치와 관계없이 프로젝트 루트에서 읽습니다.

| 변수 | 용도 |
| --- | --- |
| `BUS_USERNAME`, `BUS_PASSWORD` | 사이트 로그인 정보. 필수. |
| `DISCORD_WEBHOOK_URL` | 빈자리 또는 최종 실패 알림을 받을 웹훅 주소. 선택. |
| `LH_BUSRO_DIRECTION`, `LH_BUSRO_LINE_KEYWORD`, `LH_BUSRO_DISPATCH_TIME_KW`, `LH_BUSRO_BOARD_STATION_KW` | 조회 조건. 기본값은 각각 `in`, `부산`, `06`, `덕천역`. |
| `LH_BUSRO_HEADLESS` | `true`면 브라우저 창을 띄우지 않음. 백그라운드 스케줄러는 자동으로 `true`를 사용. |
| `LH_BUSRO_SAVE_SEATS_JSON_ON_ALERT` | `true`면 알림 대상 좌석이 있을 때 `seats.json` 저장. |
| `LH_BUSRO_DEBUG_DUMP` | `true`면 성공한 조회 화면의 진단 파일도 저장. |

## 수동 실행

```bash
/Users/jinji/Coding/.venv/bin/python main.py
```

디스코드 전송 없이 실제 사이트에서 조회만 하려면:

```bash
LH_BUSRO_HEADLESS=true /Users/jinji/Coding/.venv/bin/python main.py --no-notify
```

`main.py`는 한 번만 조회하고 종료합니다. `--no-notify`도 실제 사이트에 접속하지만 디스코드 알림은 보내지 않습니다. 웹훅을 설정하지 않아도 조회는 가능하며, 예약 확정 동작은 하지 않습니다.

## 필요할 때만 15분 간격으로 백그라운드 확인

명령어 없이 사용하려면 Finder에서 프로젝트 폴더의 `LH Busro.app`을 더블클릭하세요. 기본 브라우저에 로컬 화면이 열립니다. `15분 반복 실행`, `지금 1회 조회`, `중단` 버튼과 현재 상태·다음 조회 시각·최근 단계별 로그를 볼 수 있습니다. 화면을 닫아도 시작한 스케줄러는 계속 실행되며, 아이콘을 다시 누르면 현재 상태를 읽어 표시합니다. 화면을 여는 것만으로 조회가 시작되지는 않습니다. `중단`은 화면에서 시작한 1회 조회와 스케줄러를 중지합니다. 재부팅 후에는 아이콘을 다시 눌러 화면을 열 수 있으며 스케줄러는 자동 시작되지 않습니다.

아래는 동일한 기능을 터미널에서 사용하는 명령입니다.

```bash
/Users/jinji/Coding/.venv/bin/python manage_schedule.py start
/Users/jinji/Coding/.venv/bin/python manage_schedule.py status
/Users/jinji/Coding/.venv/bin/python manage_schedule.py stop
```

`start`는 터미널과 분리된 Python 스케줄러를 시작합니다. 즉시 한 번 조회하고 시작 시각 기준으로 15분마다 실행합니다. 터미널을 닫아도 실행되지만 재부팅·로그아웃 후에는 자동 재시작하지 않습니다. 조회는 브라우저 창 없이 실행됩니다.

알림 없는 반복 테스트는 다음 명령을 사용합니다. 이 경우에도 실제 사이트를 15분마다 조회합니다.

```bash
/Users/jinji/Coding/.venv/bin/python manage_schedule.py start --no-notify
```

이미 실행 중이라면 두 번째 `start`는 추가 스케줄러를 만들지 않습니다. 옵션을 바꾸려면 먼저 `stop`을 실행하세요.

`stop`은 스케줄러와 해당 스케줄러가 진행 중인 조회를 함께 종료합니다. 직접 실행한 `main.py`의 조회에는 영향을 주지 않습니다. Mac이 잠든 동안 빠진 회차를 몰아서 실행하지 않습니다. GitHub Actions는 수동 실행만 지원합니다.

## 디스코드 알림

알림 대상 좌석이 발견되면 `🚌 선택 가능한 좌석 발견` 제목으로 선택 가능한 좌석 번호, 전체 빈자리, 조회 화면 URL을 보냅니다. 25~28번 좌석은 알림 대상에서 제외됩니다. 대상 배차가 마감됐거나 알림 대상 좌석이 없으면 빈자리 알림을 보내지 않습니다. 최종 조회 실패 시에는 `⚠️ LH버스로 자동화 실패` 알림을 보냅니다. `--no-notify`를 사용하면 두 알림 모두 생략합니다.

실제 조회 없이 웹훅 연결만 시험하려면 아래 명령을 사용합니다. 테스트 표시가 있는 샘플 메시지 한 건을 보냅니다.

```bash
/Users/jinji/Coding/.venv/bin/python -c 'import os, main; assert os.getenv("DISCORD_WEBHOOK_URL"), "웹훅 URL이 없습니다"; raise SystemExit(0 if main.send_discord_webhook("[테스트 데이터] 가능 좌석: [17, 18]\n전체 가능: [17, 18, 25]\nURL: 샘플 메시지 — 실제 예약 링크 아님", "🧪 테스트: 선택 가능한 좌석 발견") else 1)'
```

## 로그와 실패 판정

- `logs/<실행ID>.log`: 실행 회차별 로그. 브라우저 시작·사이트 접속·로그인·노선/배차/탑승장소 선택 등 각 단계의 완료 여부와 최종 결과를 기록합니다. 오류 시 실패 단계와 traceback, 실행 코드 해시, 파일·Python 경로, 실행 출처도 남깁니다.
- `logs/<실행ID>/attempt_N.html`, `.png`: 실패한 화면의 진단 자료. 입력값을 제거/가리고 URL 쿼리와 인증정보를 로그에서 지웁니다.
- `logs/<실행ID>/seats.json`: 기존 `LH_BUSRO_SAVE_SEATS_JSON_ON_ALERT=true` 설정에서 알림 대상 좌석이 있을 때만 저장합니다.
- `logs/scheduler-YYYYMMDD.log`: 백그라운드 스케줄러의 시작·종료·회차 기록.

루트의 `lh_busro.log`는 이전 방식의 기록이며 현재 실행 결과는 `logs/<실행ID>.log`에서 확인합니다.

실행 로그·진단·스케줄러 로그는 14일 후 다음 실행에서 정리합니다. 종료 코드 `0`은 정상 조회, 마감 또는 중복 회차 생략입니다. `1`은 최종 조회 실패, 설정 오류, 시간 제한 또는 설정된 웹훅 전송 실패입니다. 로그의 마지막 결과를 함께 확인하세요.

네트워크·화면 전환 오류는 5초 후 새 브라우저에서 한 번만 재시도합니다. 로그인 거부·설정 오류·코드 오류는 즉시 실패합니다. 실패 알림은 최종 실패 때 한 번 보냅니다. 조회 프로세스는 285초에 종료하며 정리 및 알림을 포함해 약 5분 이내로 제한합니다. 알림 전송 실패 때문에 조회를 반복하지 않습니다.

## 검증

```bash
/Users/jinji/Coding/.venv/bin/python -m unittest discover -s tests -v
/Users/jinji/Coding/.venv/bin/python -m pip check
```

테스트는 로컬 HTML 및 가짜 응답으로 실행하며 실제 웹훅은 호출하지 않습니다. 실제 사이트 검증은 위 `main.py --no-notify` 명령으로 별도 실행합니다.
