"""LH버스로 좌석 조회. 예약 확정은 하지 않으며 한 회차만 실행한다."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import traceback

from dotenv import load_dotenv
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright
from playwright_stealth.stealth import Stealth
import requests

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
BASE_URL = "https://lh.busro.net:456/rsvc/"
EXCLUDED_SEATS = {25, 26, 27, 28}
RUN_ID = os.environ.get("LH_BUSRO_RUN_ID", datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
CODE_VERSION = hashlib.sha256(Path(__file__).read_bytes() + (ROOT / "runtime.py").read_bytes()).hexdigest()[:12]
ARTIFACT_DIR = ROOT / "logs" / RUN_ID


def enabled(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "y"}


def redact(value: object) -> str:
    text = str(value)
    # Strip query strings, fragments and webhook credentials before any log/notification.
    text = re.sub(r"https?://[^\s<>\"']+", lambda m: re.sub(
        r"/api/webhooks/.*", "/api/webhooks/[REDACTED]",
        m[0].split("?", 1)[0].split("#", 1)[0]), text)
    text = re.sub(r"(?i)((?:moonToken|token|password|authorization|m_pw)\s*[=:]\s*)[^\s&<>\"']+", r"\1[REDACTED]", text)
    for key in ("BUS_USERNAME", "BUS_PASSWORD", "DISCORD_WEBHOOK_URL"):
        secret = os.getenv(key)
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text


def log(message: object) -> None:
    print(f"{datetime.now().astimezone().isoformat(timespec='seconds')} | {redact(message)}", flush=True)


class ConfigError(ValueError):
    pass


class AuthenticationError(RuntimeError):
    pass


class SiteError(RuntimeError):
    pass


class ParseError(RuntimeError):
    pass


class ScheduleClosed(Exception):
    pass


class AttemptFailure(Exception):
    def __init__(self, stage: str, cause: Exception, url: str = ""):
        super().__init__(redact(f"{stage}: {type(cause).__name__}: {cause}"))
        self.stage, self.cause, self.url = stage, cause, redact(url)


@dataclass
class Result:
    status: str
    seats: dict | None = None
    url: str = ""


def _parse_dispatch_time_kw(value: str) -> tuple[int, int | None]:
    text = value.strip().replace(" ", "")
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if not match:
        match = re.fullmatch(r"(\d{1,2})시(?:(\d{1,2})분?)?", text)
    if match:
        hour, minute = int(match[1]), int(match[2]) if match[2] is not None else None
    elif re.fullmatch(r"\d{1,4}", text):
        hour, minute = (int(text), None) if len(text) <= 2 else (int(text[:-2]), int(text[-2:]))
    else:
        raise ConfigError(f"배차시간 형식 오류: {value!r}")
    if not 0 <= hour <= 23 or (minute is not None and not 0 <= minute <= 59):
        raise ConfigError(f"배차시간 범위 오류: {value!r}")
    return hour, minute


def load_config() -> dict:
    cfg = {
        "direction": (os.getenv("LH_BUSRO_DIRECTION") or "in").strip().lower(),
        "line_keyword": (os.getenv("LH_BUSRO_LINE_KEYWORD") or "부산").strip(),
        "dispatch_time_kw": (os.getenv("LH_BUSRO_DISPATCH_TIME_KW") or "06").strip(),
        "board_station_kw": (os.getenv("LH_BUSRO_BOARD_STATION_KW") or "덕천역").strip(),
    }
    if cfg["direction"] not in {"in", "out"}:
        raise ConfigError("LH_BUSRO_DIRECTION은 in 또는 out이어야 합니다")
    if not all(cfg.values()):
        raise ConfigError("조회 조건에 공백만 입력할 수 없습니다")
    _parse_dispatch_time_kw(cfg["dispatch_time_kw"])
    if not os.getenv("BUS_USERNAME") or not os.getenv("BUS_PASSWORD"):
        raise ConfigError("BUS_USERNAME, BUS_PASSWORD 환경변수가 필요합니다")
    return cfg


def close_popup_if_exists(page) -> bool:
    closed = False
    for _ in range(3):
        popup = page.locator('#mLayer_1:visible')
        if not popup.count():
            return closed
        candidates = popup.get_by_role("button", name=re.compile("닫기|close", re.I)).filter(visible=True)
        if not candidates.count():
            candidates = popup.get_by_text(re.compile("닫기|close", re.I)).filter(visible=True)
        if not candidates.count():
            candidates = popup.get_by_role("img", name=re.compile("닫기|close", re.I)).filter(visible=True)
        if not candidates.count():
            raise SiteError("보이는 공지 팝업의 닫기 컨트롤을 찾지 못했습니다")
        candidates.last.click(timeout=3000)
        page.locator("#mLayer_1").wait_for(state="hidden", timeout=3000)
        closed = True
        log("공지 팝업 닫기 완료")
    raise SiteError("공지 팝업이 반복해서 표시됩니다")


def check_page(page, *, allow_login: bool = False) -> None:
    url = page.url
    if url.startswith("chrome-error:") or "/syscon/error" in url:
        raise SiteError(f"사이트 오류 페이지: {redact(url)}")
    if not allow_login and "/login.html" in url:
        raise SiteError("로그인 세션이 유지되지 않아 로그인 화면으로 돌아왔습니다")


def wait_ready(page, predicate, description: str, *, timeout_ms: int = 15000,
               allow_login: bool = False, dialogs: list[str] | None = None) -> None:
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        check_page(page, allow_login=allow_login)
        if dialogs and any(re.search(r"비밀번호|아이디|일치하지|로그인.*실패|인증.*실패", m) for m in dialogs):
            raise AuthenticationError("사이트에서 로그인 정보를 거부했습니다")
        close_popup_if_exists(page)
        if predicate():
            return
        if time.monotonic() >= deadline:
            raise PlaywrightTimeoutError(f"{description} 확인 시간 초과; URL={redact(page.url)}")
        page.wait_for_timeout(150)


def click_and_wait(page, locator, predicate, description: str, **wait_options) -> None:
    close_popup_if_exists(page)
    try:
        locator.click(timeout=5000)
    except PlaywrightTimeoutError as error:
        # A completed click must not be submitted twice just because navigation stalled.
        if "click action done" not in str(error):
            if predicate():
                return
            if not close_popup_if_exists(page):
                raise
            locator.click(timeout=5000)
    wait_ready(page, predicate, description, **wait_options)


def select_line(page, keyword: str) -> None:
    select = page.locator('select[name="ln_idx"]')
    def matching_options():
        return [(option.get_attribute("value"), option.inner_text())
                for option in select.locator("option").all()
                if keyword in option.inner_text() and option.get_attribute("value")]
    wait_ready(page, lambda: select.is_visible() and bool(matching_options()), "노선 목록 로딩")
    matches = matching_options()
    if len(matches) != 1:
        raise ConfigError(f"노선 키워드가 여러 노선과 일치합니다: {keyword!r}")
    close_popup_if_exists(page)
    select.select_option(matches[0][0])
    wait_ready(page, lambda: select.input_value() == matches[0][0], "노선 선택")


def _extract_row_time_hm(text: str) -> tuple[int, int] | None:
    match = re.search(r"(\d{1,2})\s*(?:시\s*|:)\s*(\d{1,2})\s*분?", text)
    if match:
        return int(match[1]), int(match[2])
    match = re.search(r"(\d{1,2})\s*시", text)
    return (int(match[1]), 0) if match else None


def schedules_ready(page) -> bool:
    return any(_extract_row_time_hm(row.inner_text()) is not None
               for row in page.locator("table.bus_table2 tbody tr:visible").all())


def select_schedule_row_by_time(page, time_kw: str):
    hour, minute = _parse_dispatch_time_kw(time_kw)
    available, closed = [], []
    for row in page.locator("table.bus_table2 tbody tr:visible").all():
        hm = _extract_row_time_hm(row.inner_text())
        if hm is None or hm[0] != hour or (minute is not None and hm[1] != minute):
            continue
        if row.get_by_text("마감", exact=True).count():
            closed.append(hm)
        elif row.get_by_text("예약", exact=True).filter(visible=True).count():
            available.append((row, f"{hm[0]:02d}:{hm[1]:02d}"))
    if len(available) > 1:
        raise ConfigError("해당 시간에 배차가 여러 개입니다. HH:MM으로 지정해주세요")
    if available:
        return available[0]
    if closed:
        raise ScheduleClosed("대상 배차 마감")
    raise ConfigError(f"로드된 배차 목록에 대상 시간이 없습니다: {time_kw}")


def find_visible_row_with_radio(page, station_kw: str):
    rows = page.locator("tr:visible").filter(has_text=station_kw).filter(
        has=page.locator('input[type="radio"]:visible'))
    wait_ready(page, lambda: rows.count() > 0, "탑승장소 목록")
    if rows.count() != 1:
        raise ConfigError(f"탑승장소가 여러 행과 일치합니다: {station_kw!r}")
    return rows.first


def extract_seat_availability(page) -> dict:
    seats = {}
    for cell in page.locator('td[class*="vwSeatTd"]:visible').all():
        match = re.search(r"vwSeatTd(\d+)", cell.get_attribute("class") or "")
        if not match:
            continue
        number = int(match[1])
        checkbox = cell.locator('input[type="checkbox"]')
        if cell.locator('img[alt="예약불가"]').count():
            status = "unavailable"
        elif checkbox.count():
            status = "available" if checkbox.first.is_enabled() else "unavailable"
        else:
            status = "unknown"
        if number in seats and seats[number] != status:
            raise ParseError(f"좌석 {number} 상태가 서로 충돌합니다")
        seats[number] = status
    if not seats or "unknown" in seats.values():
        raise ParseError("좌석 화면이 비어 있거나 해석하지 못한 좌석이 있습니다")
    return {key: sorted(n for n, status in seats.items() if status == key)
            for key in ("available", "unavailable", "unknown")}


def save_diagnostics(page, attempt: int) -> None:
    """Best effort; diagnostics must never hide the original failure."""
    try:
        ARTIFACT_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as error:
        log(f"진단 디렉터리 생성 실패: {type(error).__name__}")
        return
    if page is None:
        return
    try:
        html = page.locator("html").evaluate("""element => {
            const copy = element.cloneNode(true);
            copy.querySelectorAll('script').forEach(el => el.remove());
            copy.querySelectorAll('input, textarea').forEach(el => {
                el.removeAttribute('value'); el.textContent = '';
            });
            return copy.outerHTML;
        }""", timeout=2000)
        (ARTIFACT_DIR / f"attempt_{attempt}.html").write_text(redact(html), encoding="utf-8")
    except Exception as error:
        log(f"HTML 진단 저장 실패: {type(error).__name__}")
    try:
        masks = [page.locator("input, textarea")]
        for key in ("BUS_USERNAME", "BUS_PASSWORD"):
            if os.getenv(key):
                masks.append(page.get_by_text(os.environ[key], exact=False))
        page.screenshot(path=str(ARTIFACT_DIR / f"attempt_{attempt}.png"), mask=masks, timeout=2000)
    except Exception as error:
        log(f"스크린샷 저장 실패: {type(error).__name__}")


def run_attempt(cfg: dict, attempt: int) -> Result:
    stage, page, browser = "브라우저 시작", None, None
    started = time.monotonic()
    try:
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(headless=enabled("LH_BUSRO_HEADLESS"))
                context = browser.new_context(viewport={"width": 1280, "height": 800},
                                              locale="ko-KR", timezone_id="Asia/Seoul")
                context.set_default_timeout(15000)
                context.set_default_navigation_timeout(20000)
                page = context.new_page()
                Stealth().apply_stealth_sync(page)
                log("브라우저 시작 완료")
                dialogs = []
                def on_dialog(dialog):
                    dialogs.append(dialog.message)
                    log(f"사이트 안내: {dialog.message}")
                    dialog.accept()
                page.on("dialog", on_dialog)

                stage = "사이트 접속"
                log(f"시도 {attempt}: {stage}")
                response = page.goto(BASE_URL, wait_until="domcontentloaded")
                if response is not None and response.status >= 400:
                    raise SiteError(f"사이트 HTTP 오류: {response.status}")
                log(f"{stage} 완료")
                stage = "로그인 화면"
                login_form = page.locator("#m_id")
                wait_ready(page, lambda: login_form.is_visible() or page.get_by_text("로그인", exact=True).filter(visible=True).count() > 0,
                           stage, allow_login=True)
                if not login_form.is_visible():
                    click_and_wait(page, page.get_by_text("로그인", exact=True).filter(visible=True).first,
                                   login_form.is_visible, stage, allow_login=True)
                log(f"{stage} 확인 완료")
                stage = "로그인 입력"
                login_form.fill(os.environ["BUS_USERNAME"])
                page.locator('input[type="password"]').fill(os.environ["BUS_PASSWORD"])
                log(f"{stage} 완료")
                dialogs.clear()
                stage = "로그인 완료"
                click_and_wait(page, page.get_by_role("button", name="로그인", exact=True),
                               lambda: "/login.html" not in page.url and page.locator("#ln_direct1").is_visible()
                               and page.locator('select[name="ln_idx"]').is_visible(),
                               stage, allow_login=True, dialogs=dialogs)
                log("로그인 완료")

                stage = "방향 선택"
                direction = page.locator("#ln_direct1" if cfg["direction"] == "in" else "#ln_direct2")
                if not direction.is_checked():
                    click_and_wait(page, direction, lambda: direction.is_checked() and
                                   page.locator('select[name="ln_idx"]').is_visible(), stage)
                log(f"{stage} 완료")
                stage = "노선 선택"
                select_line(page, cfg["line_keyword"])
                log(f"{stage} 완료")
                stage = "배차 조회"
                click_and_wait(page, page.get_by_role("button", name="조회", exact=True),
                               lambda: schedules_ready(page), stage)
                log(f"{stage} 완료")
                stage = "배차 선택"
                row, picked_time = select_schedule_row_by_time(page, cfg["dispatch_time_kw"])
                log(f"대상 배차 확인: {picked_time}")
                click_and_wait(page, row.get_by_text("예약", exact=True).filter(visible=True).first,
                               lambda: page.locator('tr:visible input[type="radio"]:visible').count() > 0,
                               "탑승장소 화면")
                log(f"{stage} 완료")
                stage = "탑승장소 선택"
                row = find_visible_row_with_radio(page, cfg["board_station_kw"])
                click_and_wait(page, row.locator('input[type="radio"]:visible'),
                               lambda: page.locator('#selSeatNum').is_visible() and
                               page.locator('td[class*="vwSeatTd"]:visible').count() > 0, "좌석 화면")
                log(f"{stage} 완료")
                stage = "좌석 파싱"
                seats = extract_seat_availability(page)
                log(f"{stage} 완료")
                if enabled("LH_BUSRO_DEBUG_DUMP"):
                    save_diagnostics(page, attempt)
                return Result("checked", seats, redact(page.url))
            except ScheduleClosed:
                log("배차 선택: 대상 배차 마감 확인")
                return Result("closed")
            except Exception as error:
                log(f"실패 단계={stage}, 시도={attempt}, 소요={time.monotonic() - started:.1f}초\n{traceback.format_exc()}")
                save_diagnostics(page, attempt)
                raise AttemptFailure(stage, error, page.url if page is not None else "") from error
            finally:
                if browser is not None:
                    try:
                        browser.close()
                    except Exception as error:
                        log(f"브라우저 정리 실패: {type(error).__name__}")
    except AttemptFailure:
        raise
    except Exception as error:
        raise AttemptFailure(stage, error) from error


def retryable(error: Exception) -> bool:
    if isinstance(error, (SiteError, PlaywrightTimeoutError)):
        return True
    return isinstance(error, PlaywrightError) and any(word in str(error) for word in (
        "net::ERR_", "Target closed", "Target page, context or browser has been closed", "crashed"))


def send_discord_webhook(message: str, title: str = "🚌 좌석 알림", *, notify: bool = True) -> bool:
    url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    if not notify or not url:
        log("디스코드 알림 생략 (--no-notify 또는 웹훅 미설정)")
        return True
    payload = {"username": "버스 좌석 알리미", "embeds": [{
        "title": title, "description": redact(message)[:3900],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": f"실행 {RUN_ID} · 코드 {CODE_VERSION}"},
    }]}
    try:
        response = requests.post(url, json=payload, timeout=(3, 7))
        response.raise_for_status()
        return True
    except requests.RequestException as error:
        log(f"디스코드 전송 실패: {type(error).__name__}")
        return False


def notify_failure(stage: str, error: Exception, url: str = "", *, notify: bool = True) -> None:
    message = f"실패 단계: {stage}\n{type(error).__name__}: {redact(error)[:1200]}\nURL: {redact(url)}"
    send_discord_webhook(message, "⚠️ LH버스로 자동화 실패", notify=notify)


def run_check(*, notify: bool = True) -> int:
    log(f"실행={RUN_ID} 코드={CODE_VERSION} 출처={os.getenv('LH_BUSRO_SOURCE', 'manual')} "
        f"파일={Path(__file__).resolve()} Python={sys.version.split()[0]} 인터프리터={sys.executable}")
    try:
        cfg = load_config()
        log(f"조회 조건: {cfg}")
        result = None
        for attempt in (1, 2):
            try:
                result = run_attempt(cfg, attempt)
                break
            except AttemptFailure as error:
                if attempt == 1 and retryable(error.cause):
                    log(f"일시 오류; 5초 후 새 세션으로 재시도: {error}")
                    time.sleep(5)
                    continue
                raise
        if result.status == "closed":
            log("정상 종료: 대상 배차 마감")
            return 0
        allowed = [n for n in result.seats["available"] if n not in EXCLUDED_SEATS]
        log(f"좌석 조회 성공: {result.seats}; 알림 대상={allowed}")
        if allowed and enabled("LH_BUSRO_SAVE_SEATS_JSON_ON_ALERT"):
            ARTIFACT_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
            (ARTIFACT_DIR / "seats.json").write_text(json.dumps({
                **result.seats, "excluded_seats": sorted(EXCLUDED_SEATS),
                "allowed_available_seats": allowed, "url": result.url,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        if allowed and not send_discord_webhook(
            f"가능 좌석: {allowed}\n전체 가능: {result.seats['available']}\nURL: {result.url}",
            "🚌 선택 가능한 좌석 발견", notify=notify,
        ):
            return 1
        log("정상 종료: 조회 완료")
        return 0
    except Exception as error:
        log(f"최종 실패\n{traceback.format_exc()}")
        if isinstance(error, AttemptFailure):
            notify_failure(error.stage, error.cause, error.url, notify=notify)
        else:
            notify_failure("설정/실행", error, notify=notify)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-notify", action="store_true", help="실제 조회하되 디스코드 전송 생략")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 14):
        log("Python 3.14 공유 환경을 사용하세요")
        return 1
    if args.worker:
        return run_check(notify=not args.no_notify)
    from runtime import supervise
    return supervise(ROOT, RUN_ID, no_notify=args.no_notify)


if __name__ == "__main__":
    sys.exit(main())
