"""
LH버스로 좌석 체크 - GitHub Actions / 로컬 실행 공용
환경변수(.env)에서 설정을 읽어 1회 체크 후 종료
"""

from __future__ import annotations

import os
import logging
import time
import re
import json
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from datetime import datetime
import requests

# .env 파일 로드
load_dotenv()

BASE_URL = "https://lh.busro.net:456/rsvc/"
LOG_FILE = "lh_busro.log"
HTML_DUMP_PREFIX = "lh_busro_after_select"

# 운영 모드: 평상시 파일 덤프/JSON 저장 안 함
DEBUG_DUMP = (os.getenv("LH_BUSRO_DEBUG_DUMP") or "").strip().lower() in {"1", "true", "yes", "y"}
SAVE_SEATS_JSON_ON_ALERT = (os.getenv("LH_BUSRO_SAVE_SEATS_JSON_ON_ALERT") or "").strip().lower() in {"1", "true", "yes", "y"}
RUN_ID = datetime.now().strftime("%Y%m%d_%H%M%S")

DISCORD_WEBHOOK_URL = (os.getenv("DISCORD_WEBHOOK_URL") or "").strip()
EXCLUDED_SEATS = {25, 26, 27, 28}

# 헤드리스 모드: EC2/GitHub Actions에서는 true, 로컬에서는 false
HEADLESS = (os.getenv("LH_BUSRO_HEADLESS") or "").strip().lower() in {"1", "true", "yes", "y"}


# --------------------------------------------------
# 환경변수에서 설정 읽기
# --------------------------------------------------
def load_config() -> dict:
    """환경변수에서 실행 조건을 읽어 config dict로 반환"""
    direction = (os.getenv("LH_BUSRO_DIRECTION") or "in").strip().lower()
    if direction not in {"in", "out"}:
        direction = "in"

    line_keyword = (os.getenv("LH_BUSRO_LINE_KEYWORD") or "부산").strip()
    dispatch_time_kw = (os.getenv("LH_BUSRO_DISPATCH_TIME_KW") or "06").strip().replace(" ", "")
    board_station_kw = (os.getenv("LH_BUSRO_BOARD_STATION_KW") or "덕천역").strip()

    if not dispatch_time_kw:
        raise ValueError("LH_BUSRO_DISPATCH_TIME_KW 환경변수가 비어있어요")

    return {
        "direction": direction,
        "line_keyword": line_keyword,
        "dispatch_time_kw": dispatch_time_kw,
        "board_station_kw": board_station_kw,
    }


def select_line(page, keyword: str) -> None:
    """노선 select[name=ln_idx]에서 keyword로 선택"""
    sel = page.locator('select[name="ln_idx"]')
    sel.wait_for(state="visible", timeout=8000)

    if not keyword:
        raise ValueError("노선 키워드가 필요해요")

    # option 텍스트에 keyword 포함되는 첫 option의 value를 선택
    opts = sel.locator("option")
    cnt = opts.count()
    for i in range(cnt):
        opt = opts.nth(i)
        txt = (opt.text_content() or "").strip()
        if keyword in txt:
            v = (opt.get_attribute("value") or "").strip()
            if v:
                sel.select_option(v)
                return

    raise ValueError(f"노선 키워드로 option을 못 찾았어요: keyword='{keyword}'")


# --------------------------------------------------
# 배차 시간(시 또는 시:분) 기반 배차 행 선택
# --------------------------------------------------
def _parse_dispatch_time_kw(v: str) -> tuple[int, int | None]:
    """사용자 입력 배차시간을 (hour, minute|None)으로 파싱"""
    s = (v or "").strip().replace(" ", "")

    # 1) HH:MM
    m = re.match(r"^(\d{1,2}):(\d{2})", s)
    if m:
        return int(m.group(1)), int(m.group(2))

    # 2) "18시30분" / "18시30" / "18시"
    m = re.match(r"^(\d{1,2})시(?:(\d{1,2})분?)?$", s)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2)) if m.group(2) is not None else None
        return hour, minute

    # 3) "1830" 같은 3~4자리 숫자
    m = re.match(r"^(\d{1,4})$", s)
    if m:
        digits = m.group(1)
        if len(digits) <= 2:
            return int(digits), None
        hour = int(digits[:-2])
        minute = int(digits[-2:])
        return hour, minute

    raise ValueError(f"배차시간 입력 형식이 이상해요: '{v}'")


def _extract_row_time_hm(txt: str) -> tuple[int, int] | None:
    """행 텍스트에서 (hour, minute) 추출. 못 찾으면 None."""
    t = (txt or "")

    m = re.search(r"(\d{1,2})\s*시\s*(\d{1,2})\s*분", t)
    if m:
        return int(m.group(1)), int(m.group(2))

    m = re.search(r"(\d{1,2})\s*:\s*(\d{2})", t)
    if m:
        return int(m.group(1)), int(m.group(2))

    m = re.search(r"(\d{1,2})\s*시", t)
    if m:
        return int(m.group(1)), 0

    return None


class ScheduleClosed(Exception):
    """배차가 마감된 경우"""
    pass


def select_schedule_row_by_time(page, time_kw: str):
    """배차시간(시 또는 시:분) 기준으로 예약 가능한 배차 행을 선택.
    
    반환: (row_locator, picked_time_str)
    예외: 
        - ScheduleClosed: 해당 시간 배차가 마감된 경우
        - PlaywrightTimeoutError: 배차를 못 찾거나 여러 개인 경우
    """
    want_h, want_m = _parse_dispatch_time_kw(time_kw)

    # 모든 배차 행 조회 (예약 + 마감 포함)
    all_rows = page.locator("table.bus_table2 tbody tr")
    cnt = all_rows.count()

    reserve_candidates: list[tuple[object, int, int]] = []  # 예약 가능
    closed_candidates: list[tuple[object, int, int]] = []   # 마감

    for i in range(cnt):
        row = all_rows.nth(i)
        txt = row.text_content() or ""
        hm = _extract_row_time_hm(txt)
        if hm is None:
            continue
        h, m = hm

        # 시간 매칭 확인
        time_match = False
        if want_m is None:
            if h == want_h:
                time_match = True
        else:
            if (h == want_h) and (m == want_m):
                time_match = True

        if not time_match:
            continue

        # 예약 가능 vs 마감 구분
        has_reserve = row.locator("text=예약").count() > 0
        has_closed = row.locator("text=마감").count() > 0

        if has_reserve:
            reserve_candidates.append((row, h, m))
        elif has_closed:
            closed_candidates.append((row, h, m))

    # 예약 가능한 배차가 있으면 선택
    if len(reserve_candidates) == 1:
        row, h, m = reserve_candidates[0]
        picked_time_str = f"{h:02d}:{m:02d}"
        return (row, picked_time_str)

    if len(reserve_candidates) > 1:
        times_str = ", ".join([f"{h:02d}:{m:02d}" for _, h, m in sorted(reserve_candidates, key=lambda x: (x[1], x[2]))])
        raise PlaywrightTimeoutError(f"배차가 여러 개라서 자동 선택을 중단해요: {times_str}")

    # 예약 가능한 배차가 없고, 마감된 배차가 있으면
    if len(closed_candidates) > 0:
        times_str = ", ".join([f"{h:02d}:{m:02d}" for _, h, m in sorted(closed_candidates, key=lambda x: (x[1], x[2]))])
        raise ScheduleClosed(f"마감된 배차: {times_str}")

    # 배차 자체를 못 찾음
    raise PlaywrightTimeoutError(f"배차를 못 찾았어요: 입력='{time_kw}'")


def send_discord_webhook(message: str, title: str = "🚌 좌석 알림") -> bool:
    """Discord webhook 전송. 성공 시 True."""
    if not DISCORD_WEBHOOK_URL:
        return False

    payload = {
        "username": "버스 좌석 알리미",
        "embeds": [
            {
                "title": title,
                "description": message,
                "timestamp": datetime.utcnow().isoformat(),
            }
        ],
    }

    try:
        r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        r.raise_for_status()
        return True
    except Exception as e:
        log(f"디스코드 전송 실패: {e}")
        return False


def notify_failure(stage: str, err: Exception, page_url: str = "") -> None:
    """실패/예외를 디스코드로 알림"""
    msg = (
        f"실패 단계: {stage}\n"
        f"에러: {type(err).__name__}: {err}\n"
        + (f"URL: {page_url}" if page_url else "")
    )
    send_discord_webhook(msg, title="⚠️ LH버스로 자동화 실패")


# --------------------------------------------------
# 로깅
# --------------------------------------------------
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)


def log(msg: str):
    print(msg)
    logging.info(msg)


def dump_page_html(page, prefix: str = HTML_DUMP_PREFIX, *, force: bool = False, suffix: str = "") -> str:
    """현재 페이지 HTML을 파일로 저장"""
    if not (DEBUG_DUMP or force):
        return ""

    safe_suffix = f"_{suffix}" if suffix else ""
    filename = f"{prefix}_{RUN_ID}{safe_suffix}.html"
    html = page.content()
    with open(filename, "w", encoding="utf-8") as f:
        f.write(html)
    return filename


def wait_for_seat_screen(page, timeout_ms: int = 10000) -> None:
    """좌석선택 영역(#selSeatNum)이 채워질 때까지 대기"""
    deadline = time.monotonic() + (timeout_ms / 1000.0)

    sel = page.locator("#selSeatNum")
    while True:
        try:
            if sel.count() > 0 and sel.first.is_visible():
                txt = (sel.first.text_content() or "").strip()
                if ("좌석위치 선택" in txt) or ("다음단계" in txt) or ("탑승인원 정보" in txt):
                    return
        except Exception:
            pass

        if time.monotonic() > deadline:
            raise PlaywrightTimeoutError("좌석 선택 화면 대기 실패(#selSeatNum)")

        page.wait_for_timeout(200)


def extract_seat_availability(page) -> dict:
    """좌석번호별 선택 가능 여부를 파싱"""
    seats: dict[int, str] = {}

    tds = page.locator("td[class*='vwSeatTd']")
    cnt = 0
    try:
        cnt = tds.count()
    except Exception:
        cnt = 0

    for i in range(cnt):
        td = tds.nth(i)
        try:
            cls = td.get_attribute("class") or ""
            m = re.search(r"vwSeatTd(\d+)", cls)
            if not m:
                continue
            seat_no = int(m.group(1))

            has_checkbox = td.locator("input[type='checkbox']").count() > 0
            has_blocked_img = td.locator("img[alt='예약불가']").count() > 0

            status = "unknown"
            if has_checkbox:
                status = "available"
            elif has_blocked_img:
                status = "unavailable"

            prev = seats.get(seat_no)
            if prev is None:
                seats[seat_no] = status
            else:
                rank = {"available": 3, "unavailable": 2, "unknown": 1}
                if rank[status] > rank[prev]:
                    seats[seat_no] = status
        except Exception:
            continue

    available = sorted([n for n, s in seats.items() if s == "available"])
    unavailable = sorted([n for n, s in seats.items() if s == "unavailable"])
    unknown = sorted([n for n, s in seats.items() if s == "unknown"])

    return {"available": available, "unavailable": unavailable, "unknown": unknown}


# --------------------------------------------------
# 유틸
# --------------------------------------------------
def close_popup_if_exists(page) -> bool:
    """팝업이 있으면 닫기"""
    try:
        close_btn = page.locator("text=닫기").first
        if close_btn.is_visible(timeout=1500):
            close_btn.click()
            log("   → 팝업 닫음!")
            return True
    except Exception:
        pass
    return False


def wait_for_boarding_screen(page, timeout_ms: int = 8000) -> None:
    """탑승/하차 장소 선택 영역 진입을 확정"""
    deadline = time.monotonic() + (timeout_ms / 1000.0)

    headers = [
        page.get_by_text("탑승장소를 선택해주세요."),
        page.get_by_text("탑승장소를 선택해주세요"),
        page.get_by_text("하차장소를 선택해주세요."),
        page.get_by_text("하차장소를 선택해주세요"),
    ]

    last_debug = ""

    while True:
        try:
            bw_visible_radio = page.locator("tr.bwRow:visible input[type='radio']:visible")
            if bw_visible_radio.count() > 0:
                return
        except Exception:
            pass

        try:
            for h in headers:
                cnt = h.count()
                for i in range(cnt):
                    if h.nth(i).is_visible():
                        return
            last_debug = f"headers_checked={len(headers)}"
        except Exception:
            pass

        if time.monotonic() > deadline:
            raise PlaywrightTimeoutError(
                f"탑승/하차 장소 선택 화면 대기 실패 ({last_debug})"
            )

        page.wait_for_timeout(200)


def find_visible_row_with_radio(page, station_kw: str, timeout_ms: int = 8000):
    """station_kw를 '포함'하는 행 중, 보이는 라디오가 있는 행을 찾아 반환"""
    deadline = time.monotonic() + (timeout_ms / 1000.0)

    pattern_station = re.compile(re.escape(station_kw))

    while True:
        rows = page.locator("tr")
        try:
            cnt = rows.count()
        except Exception:
            cnt = 0

        for i in range(cnt):
            r = rows.nth(i)
            try:
                if not r.is_visible():
                    continue

                txt = (r.text_content() or "")
                if not pattern_station.search(txt):
                    continue

                radio = r.locator('input[type="radio"]:visible').first
                if radio.count() == 0:
                    continue

                return r
            except Exception:
                continue

        if time.monotonic() > deadline:
            raise PlaywrightTimeoutError(
                f"대상 행 탐색 실패: station_kw='{station_kw}'"
            )

        page.wait_for_timeout(200)


def main():
    username = os.getenv("BUS_USERNAME")
    password = os.getenv("BUS_PASSWORD")

    if not username or not password:
        log("오류: BUS_USERNAME, BUS_PASSWORD 환경변수를 설정하세요!")
        return

    cfg = load_config()
    excluded_seats = set(EXCLUDED_SEATS)

    log("=== 실행 조건 ===")
    log(f"- 방향: {cfg['direction']}")
    log(f"- 노선: keyword={cfg['line_keyword']}")
    log(f"- 배차시간 입력: {cfg['dispatch_time_kw']}")
    log(f"- 탑승장소 키워드: {cfg['board_station_kw']}")
    log(f"- 알림 제외 좌석: {sorted(excluded_seats)}")
    log(f"- 헤드리스 모드: {HEADLESS}")

    log(f"로그인 정보: {username} / {'*' * len(password)}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        page = browser.new_page()

        def handle_dialog(dialog):
            log(f"   ★ Alert 감지! 메시지: {dialog.message}")
            dialog.accept()

        page.on("dialog", handle_dialog)

        try:
            # 1. 사이트 접속
            log("\n1. 사이트 접속...")
            page.goto(BASE_URL)
            page.wait_for_timeout(1000)

            # 2. 팝업 닫기
            log("2. 팝업 확인...")
            close_popup_if_exists(page)
            page.wait_for_timeout(1000)

            # 3. 로그인 버튼 클릭
            log("3. 로그인 버튼 클릭...")
            page.locator("text=로그인").first.click()
            page.wait_for_timeout(1000)

            # 4. 로그인 정보 입력
            log("4. 로그인 정보 입력...")
            page.locator("#m_id").fill(username)
            page.locator('input[type="password"]').first.fill(password)
            page.wait_for_timeout(500)

            # 5. 로그인 제출
            log("5. 로그인 제출...")
            page.locator('button:has-text("로그인")').click()
            page.wait_for_timeout(2000)

            # 6. 팝업 확인
            log("6. 팝업 확인...")
            close_popup_if_exists(page)
            page.wait_for_timeout(1000)

            # 7. 방향 선택 (진주도착/진주출발)
            log("7. 방향 선택...")
            if cfg["direction"] == "in":
                page.locator("#ln_direct1").click()
                log("   → 진주도착 선택 완료!")
            else:
                page.locator("#ln_direct2").click()
                log("   → 진주출발 선택 완료!")
            page.wait_for_timeout(1000)

            # 8. 팝업 확인
            log("8. 팝업 확인...")
            close_popup_if_exists(page)
            page.wait_for_timeout(1000)

            # 9. 노선 선택
            log("9. 노선 선택...")
            try:
                select_line(page, keyword=cfg["line_keyword"])
                log("   → 노선 선택 완료!")
            except Exception as e:
                log(f"   → 노선 선택 실패: {e}")
                notify_failure("노선 선택", e, page.url if page else "")
                dump_page_html(page, force=True, suffix="fail_step9")
                return
            page.wait_for_timeout(500)

            # 10. 조회 버튼 클릭
            log("10. 조회 버튼 클릭...")
            page.locator('button:has-text("조회")').click()
            log("   → 조회 클릭 완료!")
            page.wait_for_timeout(3000)

            # 11. 팝업 확인
            log("11. 팝업 확인...")
            popup = page.locator("#mLayer_1")
            if popup.is_visible():
                log("   → mLayer_1 팝업 보임!")
            else:
                log("   → mLayer_1 팝업 안 보임")

            close_popup_if_exists(page)
            page.wait_for_timeout(1000)

            # 12. 예약 버튼 클릭 (선택한 배차 시간대)
            log(f"\n12. 배차시간 '{cfg['dispatch_time_kw']}' 예약 버튼 찾기...")

            try:
                row, picked_time = select_schedule_row_by_time(page, cfg["dispatch_time_kw"])
                reserve_btn = row.locator("text=예약").first
                reserve_btn.click()
                log(f"   → {picked_time} 예약 버튼 클릭!")

                wait_for_boarding_screen(page, timeout_ms=8000)
                log("   → 탑승장소 선택 화면 진입 확인!")

            except ScheduleClosed as e:
                log(f"   → ❌ 빈자리 없음 (마감): {e}")
                log("=== 빈자리 없음 - 종료 ===")
                return

            except Exception as e:
                log(f"   → 배차시간 '{cfg['dispatch_time_kw']}' 예약 버튼 처리 실패: {e}")
                notify_failure(f"배차시간 '{cfg['dispatch_time_kw']}' 예약 버튼", e, page.url if page else "")
                dump_page_html(page, force=True, suffix="fail_step12")
                return

            page.wait_for_timeout(1000)

            # 13. 탑승장소 라디오 클릭
            log(f"\n13. {cfg['board_station_kw']} 선택박스(radio) 찾기...")

            try:
                target_row = find_visible_row_with_radio(
                    page,
                    station_kw=cfg["board_station_kw"],
                    timeout_ms=8000,
                )

                radio = target_row.locator('input[type="radio"]:visible').first
                radio.click(force=True)

                log(f"   → {cfg['board_station_kw']} 라디오 클릭 완료!")

                try:
                    page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass
                page.wait_for_timeout(1500)

                # 좌석 선택 화면 진입 대기 + 좌석 파싱
                try:
                    wait_for_seat_screen(page, timeout_ms=12000)
                    seat_info = extract_seat_availability(page)

                    log("=== 좌석 선택 가능 여부 ===")
                    log(f"- available ({len(seat_info['available'])}): {seat_info['available']}")
                    log(f"- unavailable ({len(seat_info['unavailable'])}): {seat_info['unavailable']}")
                    if seat_info["unknown"]:
                        log(f"- unknown ({len(seat_info['unknown'])}): {seat_info['unknown']}")

                    available_seats = sorted([int(x) for x in seat_info.get("available", [])])
                    allowed_available = [s for s in available_seats if s not in excluded_seats]

                    if allowed_available:
                        msg = (
                            f"제외좌석({sorted(excluded_seats)}) 빼고 선택 가능한 좌석이 있어요.\n"
                            f"- 가능 좌석: {allowed_available}\n"
                            f"- 전체 가능: {available_seats}\n"
                            f"- URL: {page.url}"
                        )
                        ok = send_discord_webhook(msg, title="🚌 선택 가능한 좌석 발견")
                        if ok:
                            log("=== 디스코드 알림 전송 완료 ===")
                        else:
                            log("=== 디스코드 알림 전송 실패(또는 WEBHOOK 미설정) ===")
                    else:
                        log("=== 알림 스킵: 제외좌석만 가능하거나 가능좌석 없음 ===")

                    if SAVE_SEATS_JSON_ON_ALERT and allowed_available:
                        seat_json = f"lh_busro_seats_{RUN_ID}.json"
                        with open(seat_json, "w", encoding="utf-8") as f:
                            out = dict(seat_info)
                            out["excluded_seats"] = sorted(excluded_seats)
                            out["available_seats"] = available_seats
                            out["allowed_available_seats"] = allowed_available
                            out["url"] = page.url
                            json.dump(out, f, ensure_ascii=False, indent=2)
                        log(f"=== 좌석 파싱 JSON 저장 완료: {seat_json} ===")

                except Exception as e:
                    log(f"좌석 화면/파싱 실패(건너뜀): {e}")

                dump_file = dump_page_html(page)
                if dump_file:
                    log(f"=== 다음 화면 HTML 저장 완료: {dump_file} ===")
                log(f"현재 URL: {page.url}")

            except Exception as e:
                log(f"   → {cfg['board_station_kw']} 라디오 클릭 실패: {e}")
                notify_failure(f"{cfg['board_station_kw']} 선택", e, page.url if page else "")
                dump_page_html(page, force=True, suffix="fail_step13")

        finally:
            try:
                browser.close()
            except Exception:
                pass
            log("완료!")


if __name__ == "__main__":
    main()