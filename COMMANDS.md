# LH버스로 - 간단한 명령어

## 🚀 시작

```bash
bash /Users/jinhyeok/coding/LHbusro/install_schedule.sh
```

그 이후로는 **자동으로 15분마다 실행됩니다.**

---

## 🛑 정지

```bash
bash /Users/jinhyeok/coding/LHbusro/uninstall_schedule.sh
```

---

## 📊 로그 보기

### 최신 실행 로그 확인

```bash
ls -t /Users/jinhyeok/coding/LHbusro/logs/ | head -1 | xargs -I {} cat /Users/jinhyeok/coding/LHbusro/logs/{}
```

### 전체 실행 이력 확인 (lh_busro.log)

```bash
cat /Users/jinhyeok/coding/LHbusro/lh_busro.log
```

### 오류만 필터링

```bash
grep "❌\|ERROR\|에러" /Users/jinhyeok/coding/LHbusro/lh_busro.log
```

### 로그 폴더 열기

```bash
open /Users/jinhyeok/coding/LHbusro/logs/
```

---

## 🧪 한 번만 실행 (테스트)

```bash
bash /Users/jinhyeok/coding/LHbusro/run_busro.sh
```

---

## ✅ 잘 돌아가는지 확인

```bash
launchctl list | grep lhbusro
```

결과에 `com.lhbusro.checker` 가 보이면 ✅ OK

---

---

## ⚠️ 오류 발생 시

### 오류 증상
- 로그에 `1. 사이트 접속...` 후 바로 `완료!`로 끝나는 경우
- Discord에 `⚠️ LH버스로 자동화 실패` 알림이 오는 경우

### 원인 및 해결

| 오류 | 원인 | 해결 |
|------|------|------|
| `net::ERR_INTERNET_DISCONNECTED` | 인터넷 연결 끊김 | 자동으로 5초 대기 후 2회 재시도함 |
| 3회 모두 실패 | 장시간 인터넷 불안정 | 네트워크 확인 후 다음 실행 대기 |

### 상세 오류 확인

```bash
# 최근 실행 로그에서 에러 확인
ls -t /Users/jinhyeok/coding/LHbusro/logs/ | head -3
cat /Users/jinhyeok/coding/LHbusro/logs/<위에서_나온_파일명>
```

---

끝!
