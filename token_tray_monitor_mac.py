"""
Claude Code 토큰 사용량 트레이 모니터 (macOS 버전)

macOS 메뉴 막대(상단 시계 옆)에 신호등 모양 아이콘을 띄워서
- 실제 계정의 세션(5시간)·주간 한도 잔여율
- 오늘 하루 누적 토큰 수 및 대략적인 비용 추정치
을 실시간으로 보여준다.

데이터 출처: ~/.claude/projects/**/*.jsonl (Claude Code가 세션마다 남기는 로컬 로그)
인터넷 접속이나 API 키가 필요 없다.

실행: 터미널에서 `python3 token_tray_monitor_mac.py` (더블클릭 실행은 지원 안 함 —
macOS는 .pyw/.py 파일 확장자에 대한 실행 연결이 없음. run_mac.command로 더블클릭 가능)
"""

import glob
import json
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pystray
from PIL import Image, ImageDraw, ImageFont
from pystray import MenuItem as Item

# ---------------------------------------------------------------------------
# 설정 (필요하면 여기 숫자만 바꾸면 됨)
# ---------------------------------------------------------------------------
CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
CONTEXT_WINDOW = 200_000       # 세션 컨텍스트 기준치. 1M 컨텍스트 베타 사용 중이면 늘려도 됨
POLL_SECONDS = 5               # 몇 초마다 로그를 다시 읽을지
TAIL_BYTES_FOR_SESSION = 300_000  # "현재 세션" 파악용으로 파일 끝에서 얼마나 읽을지
USAGE_POLL_SECONDS = 180       # 실제 계정 사용률(세션/주간 한도)은 몇 초마다 다시 물어볼지


def _find_claude_cli():
    exe = shutil.which("claude")
    if exe:
        return exe
    fallback = Path.home() / ".local" / "bin" / "claude"
    if fallback.exists():
        return str(fallback)
    return None


CLAUDE_CLI = _find_claude_cli()

# 모델별 $ / 1M 토큰 (input, output). 실제 청구서와 다를 수 있는 추정치.
PRICING = {
    "claude-sonnet-5": (2.00, 10.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-fable-5": (10.00, 50.00),
    "claude-mythos-5": (10.00, 50.00),
}
DEFAULT_PRICING = (3.00, 15.00)  # 표에 없는 모델일 때 대략값
CACHE_WRITE_MULT = 1.25   # 캐시 생성 토큰은 입력가의 약 1.25배
CACHE_READ_MULT = 0.10    # 캐시 재사용 토큰은 입력가의 약 0.1배

# ---------------------------------------------------------------------------
# 상태 (여러 스레드가 공유, _lock으로 보호)
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_state = {
    "context_pct": 0.0,
    "context_tokens": 0,
    "today_tokens": 0,
    "today_cost": 0.0,
    "error": None,
    "session_used_pct": None,   # 실제 계정 "세션(5시간)" 한도 사용률(%). claude CLI /usage로 갱신
    "session_resets": None,
    "weekly_used_pct": None,    # 실제 계정 "주간" 한도 사용률(%)
    "weekly_resets": None,
    "usage_error": None,
}
_file_offsets = {}   # path -> 다음에 읽을 바이트 위치
_today_key = None    # "YYYY-MM-DD" (로컬 기준), 날짜 바뀌면 초기화용


def _all_jsonl_files():
    pattern = str(CLAUDE_PROJECTS_DIR / "**" / "*.jsonl")
    return glob.glob(pattern, recursive=True)


def _find_active_session_file():
    files = _all_jsonl_files()
    if not files:
        return None
    return max(files, key=lambda p: os.path.getmtime(p))


def _usage_from_line(line):
    """usage가 있는 assistant 메시지 라인이면 (model, usage dict, 로컬 date) 반환."""
    if '"usage"' not in line:
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    message = obj.get("message")
    if not isinstance(message, dict):
        return None
    usage = message.get("usage")
    if not usage:
        return None
    model = message.get("model")
    ts = obj.get("timestamp")
    local_date = None
    if ts:
        try:
            dt_utc = datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
            local_date = dt_utc.astimezone().date().isoformat()
        except ValueError:
            pass
    return model, usage, local_date


def _usage_cost(model, usage):
    in_price, out_price = PRICING.get(model, DEFAULT_PRICING)
    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)
    cache_creation = usage.get("cache_creation_input_tokens", 0)
    cache_read = usage.get("cache_read_input_tokens", 0)
    cost = (
        input_tokens * in_price
        + output_tokens * out_price
        + cache_creation * in_price * CACHE_WRITE_MULT
        + cache_read * in_price * CACHE_READ_MULT
    ) / 1_000_000
    return cost


def _usage_token_count(usage):
    return (
        usage.get("input_tokens", 0)
        + usage.get("output_tokens", 0)
        + usage.get("cache_creation_input_tokens", 0)
        + usage.get("cache_read_input_tokens", 0)
    )


def _read_last_usage(path):
    """파일 끝부분만 읽어서 마지막 usage 기록을 찾는다 (현재 세션 컨텍스트용)."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > TAIL_BYTES_FOR_SESSION:
                f.seek(size - TAIL_BYTES_FOR_SESSION)
            data = f.read()
    except OSError:
        return None, None
    text = data.decode("utf-8", errors="ignore")
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        parsed = _usage_from_line(line)
        if parsed:
            model, usage, _ = parsed
            return model, usage
    return None, None


def _today_local_key():
    return datetime.now().astimezone().date().isoformat()


def _scan_file_for_today(path, start_offset, today_key):
    """start_offset부터 끝까지 읽어서 오늘 날짜 usage만 (토큰, 비용) 합산. 새 오프셋 반환."""
    try:
        with open(path, "rb") as f:
            f.seek(start_offset)
            data = f.read()
    except OSError:
        return start_offset, 0, 0.0

    if not data:
        return start_offset, 0, 0.0

    text = data.decode("utf-8", errors="ignore")
    ends_with_newline = text.endswith("\n")
    lines = text.split("\n")
    if not ends_with_newline:
        partial = lines.pop()
        new_offset = start_offset + len(data) - len(partial.encode("utf-8"))
    else:
        if lines and lines[-1] == "":
            lines.pop()
        new_offset = start_offset + len(data)

    tokens = 0
    cost = 0.0
    for line in lines:
        parsed = _usage_from_line(line)
        if not parsed:
            continue
        model, usage, local_date = parsed
        if local_date != today_key:
            continue
        tokens += _usage_token_count(usage)
        cost += _usage_cost(model, usage)
    return new_offset, tokens, cost


def _reset_today(today_key):
    """자정이 지났거나 프로그램을 막 켰을 때: 오늘치를 처음부터 다시 집계."""
    global _file_offsets
    _file_offsets = {}
    total_tokens = 0
    total_cost = 0.0
    for path in _all_jsonl_files():
        new_offset, tokens, cost = _scan_file_for_today(path, 0, today_key)
        _file_offsets[path] = new_offset
        total_tokens += tokens
        total_cost += cost
    return total_tokens, total_cost


def _poll_loop():
    global _today_key
    today_total_tokens = 0
    today_total_cost = 0.0

    while True:
        try:
            today_key = _today_local_key()
            if today_key != _today_key:
                today_total_tokens, today_total_cost = _reset_today(today_key)
                _today_key = today_key
            else:
                for path in _all_jsonl_files():
                    start = _file_offsets.get(path, 0)
                    new_offset, tokens, cost = _scan_file_for_today(path, start, today_key)
                    _file_offsets[path] = new_offset
                    today_total_tokens += tokens
                    today_total_cost += cost

            active_path = _find_active_session_file()
            context_tokens = 0
            if active_path:
                _model, usage = _read_last_usage(active_path)
                if usage:
                    context_tokens = (
                        usage.get("input_tokens", 0)
                        + usage.get("cache_read_input_tokens", 0)
                        + usage.get("cache_creation_input_tokens", 0)
                    )
            pct = min(100.0, context_tokens / CONTEXT_WINDOW * 100)

            with _lock:
                _state["context_pct"] = pct
                _state["context_tokens"] = context_tokens
                _state["today_tokens"] = today_total_tokens
                _state["today_cost"] = today_total_cost
                _state["error"] = None
        except Exception as exc:  # 트레이 아이콘은 죽으면 안 되니 뭐가 터져도 계속 돈다
            with _lock:
                _state["error"] = str(exc)

        time.sleep(POLL_SECONDS)


# ---------------------------------------------------------------------------
# 실제 계정 사용률 (세션 5시간 / 주간 한도) — claude CLI의 /usage를 그대로 물어본다.
# 모델을 호출하지 않는 로컬 슬래시 명령이라 토큰/비용이 들지 않는다.
# ---------------------------------------------------------------------------
_SESSION_USAGE_RE = re.compile(
    r"Current session[^:\n]*:\s*(\d+)%\s*used(?:\s*(?:·|-)\s*resets\s*([^\n]+))?",
    re.IGNORECASE,
)
_WEEKLY_USAGE_RE = re.compile(
    r"Current week[^:\n]*:\s*(\d+)%\s*used(?:\s*(?:·|-)\s*resets\s*([^\n]+))?",
    re.IGNORECASE,
)


def _fetch_usage_via_cli():
    """claude CLI를 비대화식으로 실행해 /usage 출력을 파싱한다.
    반환: (dict | None, 오류메시지 | None)"""
    if not CLAUDE_CLI:
        return None, "claude CLI를 찾을 수 없음 (PATH 확인 필요)"
    try:
        result = subprocess.run(
            [CLAUDE_CLI, "-p", "/usage", "--output-format", "text"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except Exception as exc:
        return None, str(exc)

    text = result.stdout or ""
    session_match = _SESSION_USAGE_RE.search(text)
    weekly_match = _WEEKLY_USAGE_RE.search(text)
    if not session_match and not weekly_match:
        return None, "/usage 출력 형식을 인식하지 못함"

    return {
        "session_used_pct": int(session_match.group(1)) if session_match else None,
        "session_resets": (session_match.group(2).strip() if session_match and session_match.group(2) else None),
        "weekly_used_pct": int(weekly_match.group(1)) if weekly_match else None,
        "weekly_resets": (weekly_match.group(2).strip() if weekly_match and weekly_match.group(2) else None),
    }, None


def _usage_poll_loop():
    while True:
        data, err = _fetch_usage_via_cli()
        with _lock:
            if data:
                _state.update(data)
                _state["usage_error"] = None
            else:
                _state["usage_error"] = err
        time.sleep(USAGE_POLL_SECONDS)


# ---------------------------------------------------------------------------
# 아이콘 그리기 (신호등 모양 + 잔량 % 숫자)
# ---------------------------------------------------------------------------
_FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Verdana Bold.ttf",
    "arialbd.ttf",
]
_font_cache = {}


def _load_font(size):
    if size in _font_cache:
        return _font_cache[size]
    for name in _FONT_CANDIDATES:
        try:
            font = ImageFont.truetype(name, size)
            _font_cache[size] = font
            return font
        except OSError:
            continue
    font = ImageFont.load_default()
    _font_cache[size] = font
    return font


def _traffic_color(remaining_pct):
    if remaining_pct > 40:
        return (46, 204, 113, 255)   # 초록: 여유 많음
    if remaining_pct > 15:
        return (241, 196, 15, 255)   # 노랑: 슬슬 채워짐
    return (231, 76, 60, 255)        # 빨강: 여유 얼마 안 남음


def make_icon_image(remaining_pct, size=256, error=False, unknown=False):
    """remaining_pct: 실제 계정 세션(5시간) 한도의 '남은' 비율(0~100). 신호등 색 + 가운데 숫자로 표시."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    pad = int(size * 0.02)
    x0, y0, x1, y1 = pad, pad, size - pad, size - pad
    diameter = x1 - x0

    color = (140, 140, 140, 255) if (error or unknown) else _traffic_color(remaining_pct)
    draw.ellipse([x0, y0, x1, y1], fill=color)

    text = "!" if error else ("…" if unknown else str(int(round(remaining_pct))))
    font_size = int(diameter * 0.75)
    font = _load_font(font_size)
    while font_size > 10:
        font = _load_font(font_size)
        bbox = draw.textbbox((0, 0), text, font=font)
        w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        if w <= diameter * 0.88 and h <= diameter * 0.88:
            break
        font_size -= 4

    bbox = draw.textbbox((0, 0), text, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    tx = x0 + (diameter - w) / 2 - bbox[0]
    ty = y0 + (diameter - h) / 2 - bbox[1]
    draw.text((tx, ty), text, font=font, fill=(255, 255, 255, 255))
    return img


def _build_tooltip(s):
    if s["error"]:
        return f"토큰 모니터 오류: {s['error'][:80]}"

    lines = []
    if s["session_used_pct"] is not None:
        remain = 100 - s["session_used_pct"]
        resets = f" (초기화 {s['session_resets']})" if s["session_resets"] else ""
        lines.append(f"세션 한도 잔여 {remain}%{resets}")
    elif s["usage_error"]:
        lines.append(f"세션 한도 확인 실패: {s['usage_error'][:50]}")
    else:
        lines.append("세션 한도 확인 중...")

    if s["weekly_used_pct"] is not None:
        remain = 100 - s["weekly_used_pct"]
        resets = f" (초기화 {s['weekly_resets']})" if s["weekly_resets"] else ""
        lines.append(f"주간 한도 잔여 {remain}%{resets}")

    lines.append(f"오늘 누적 {s['today_tokens']:,} 토큰 (약 ${s['today_cost']:.2f})")
    return "\n".join(lines)


def _menu_text_session(_item):
    with _lock:
        pct = _state["session_used_pct"]
        resets = _state["session_resets"]
        err = _state["usage_error"]
    if pct is None:
        return f"세션 한도: 확인 중{' (' + err[:30] + ')' if err else '...'}"
    resets_txt = f" · 초기화 {resets}" if resets else ""
    return f"세션 한도 잔여 {100 - pct}% (사용 {pct}%){resets_txt}"


def _menu_text_weekly(_item):
    with _lock:
        pct = _state["weekly_used_pct"]
        resets = _state["weekly_resets"]
    if pct is None:
        return "주간 한도: 확인 중..."
    resets_txt = f" · 초기화 {resets}" if resets else ""
    return f"주간 한도 잔여 {100 - pct}% (사용 {pct}%){resets_txt}"


# ---------------------------------------------------------------------------
# 트레이 아이콘 실행
# ---------------------------------------------------------------------------
def _on_quit(icon, _item):
    icon.stop()


def run():
    icon = pystray.Icon(
        "claude_token_monitor",
        make_icon_image(0, unknown=True),
        "Claude 사용량 확인 중...",
    )
    icon.menu = pystray.Menu(
        Item(_menu_text_session, None, enabled=False),
        Item(_menu_text_weekly, None, enabled=False),
        pystray.Menu.SEPARATOR,
        Item("종료", _on_quit),
    )

    threading.Thread(target=_poll_loop, daemon=True).start()
    threading.Thread(target=_usage_poll_loop, daemon=True).start()

    def _updater():
        while True:
            with _lock:
                s = dict(_state)
            has_error = bool(s["error"])
            session_pct = s["session_used_pct"]
            unknown = session_pct is None and not has_error
            remaining_pct = 100.0 - session_pct if session_pct is not None else 0.0
            icon.icon = make_icon_image(remaining_pct, error=has_error, unknown=unknown)
            icon.title = _build_tooltip(s)
            icon.update_menu()
            time.sleep(POLL_SECONDS)

    threading.Thread(target=_updater, daemon=True).start()
    icon.run()


if __name__ == "__main__":
    run()
