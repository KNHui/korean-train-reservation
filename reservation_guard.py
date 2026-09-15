"""Continuous polling with optional limits and reconciled reservation retries.

The transport budget covers internal library calls as well as public operations.
All client imports and credential access are deferred until an explicit run.
"""

import argparse
from collections import deque
from contextlib import contextmanager, redirect_stdout
from dataclasses import dataclass
from datetime import datetime
from getpass import getpass
import hashlib
import io
import json
import math
import os
from pathlib import Path
import random
import time


RUNTIME = Path(__file__).resolve().parent / ".runtime"


class StopRun(Exception):
    """A local limit or server restriction requires stopping this run."""


class LoginFailure(StopRun):
    """Only local, allowlisted reasons may reach the terminal or audit log."""

    MESSAGES = {
        "macro_blocked": "서버가 자동화 접근을 제한했습니다 (MACRO ERROR). "
                         "현재 클라이언트의 인증 호환성을 확인해야 합니다. 공식 앱을 이용하세요.",
        "encryption_failed": "로그인 암호화 정보 요청이 거절됐습니다. 클라이언트 인증 호환성을 확인하세요.",
        "server_rejected": "서버가 로그인을 승인하지 않았습니다. 계정 오류 여부는 확인되지 않았습니다.",
        "invalid_response": "로그인 응답 형식이 예상과 다릅니다. 클라이언트 API 호환성을 확인하세요.",
        "continuation_required": "로그인에 추가 인증이 필요합니다. 공식 앱에서 인증을 완료하세요.",
    }

    def __init__(self, reason):
        self.reason = reason
        super().__init__(self.MESSAGES[reason])


@dataclass(frozen=True)
class Policy:
    interval: float = 15
    max_searches: int | None = None
    max_seconds: float | None = None
    max_requests: int | None = None
    max_errors: int = 3
    max_interval: float = 30

    def validate(self):
        if not math.isfinite(self.interval) or self.interval < 15:
            raise ValueError("최소 조회 간격은 15초 이상이어야 합니다.")
        if not math.isfinite(self.max_interval) or self.max_interval < self.interval:
            raise ValueError("최대 조회 간격은 최소 조회 간격 이상이어야 합니다.")
        if self.max_seconds is not None and (
                not math.isfinite(self.max_seconds) or self.max_seconds <= 0):
            raise ValueError("실행 시간은 양수여야 합니다.")
        if any(value is not None and value < 1
               for value in (self.max_searches, self.max_requests, self.max_errors)):
            raise ValueError("횟수 제한은 1 이상이어야 합니다.")

    def limit_summary(self):
        searches = f"최대 {self.max_searches}회" if self.max_searches is not None else "무제한"
        duration = f"최대 {self.max_seconds / 60:g}분" if self.max_seconds is not None else "무제한"
        requests = f"최대 {self.max_requests}회" if self.max_requests is not None else "무제한"
        return f"조회 {searches} · 시간 {duration} · HTTP {requests}"


class IntervalQueue:
    """Keep ten upcoming waits, balancing new samples against the queued mean."""

    size = 10

    def __init__(self, policy, rng=None):
        policy.validate()
        self.minimum, self.maximum = policy.interval, policy.max_interval
        self.target = (self.minimum + self.maximum) / 2
        self.rng = rng if rng is not None else random.Random()
        self.pending = deque()
        for _ in range(self.size):
            self._append()

    def _append(self):
        low, high = self.minimum, self.maximum
        if self.pending:
            average = sum(self.pending) / len(self.pending)
            if average < self.target:
                low = self.target
            elif average > self.target:
                high = self.target
        self.pending.append(self.rng.uniform(low, high))

    def next_delay(self):
        delay = self.pending.popleft()
        self._append()
        return delay


@dataclass(frozen=True)
class Query:
    rail: str
    departure: str
    arrival: str
    date: str
    start: str
    end: str
    train_number: str = ""
    reserve: bool = False
    adults: int = 1
    children: int = 0
    seniors: int = 0
    disability1to3: int = 0
    disability4to6: int = 0
    seat: str = "general"

    def validate(self):
        if self.rail != "KTX":
            raise ValueError("KTX만 지원합니다.")
        for value, length, fmt in ((self.date, 8, "%Y%m%d"),
                                   (self.start, 6, "%H%M%S"),
                                   (self.end, 6, "%H%M%S")):
            if len(value) != length or not value.isdigit():
                raise ValueError("날짜는 YYYYMMDD, 시각은 HHMMSS 형식입니다.")
            datetime.strptime(value, fmt)
        if self.date < datetime.now().strftime("%Y%m%d") or self.start > self.end:
            raise ValueError("과거 날짜 또는 뒤바뀐 시간 범위입니다.")
        if not self.departure or not self.arrival or self.departure == self.arrival:
            raise ValueError("서로 다른 출발역과 도착역을 입력하세요.")
        if self.train_number and not self.train_number.isdigit():
            raise ValueError("열차 번호는 숫자로 입력하세요.")
        counts = (self.adults, self.children, self.seniors, self.disability1to3, self.disability4to6)
        if min(counts) < 0 or sum(counts) < 1:
            raise ValueError("승객 수는 음수일 수 없으며 최소 1명이 필요합니다.")
        if sum(counts) > 9:
            raise ValueError("KTX 예약은 최대 9명까지 가능합니다.")
        if self.seat not in ("general", "special", "general-first", "special-first"):
            raise ValueError("좌석 조건을 확인하세요.")


class Audit:
    """Allowlisted telemetry: never serialize exceptions, URLs or API payloads."""

    def __init__(self, path):
        self.path = path

    def __call__(self, event, **fields):
        allowed = {"operation", "elapsed", "count", "status", "error_type", "state"}
        row = {"time": datetime.now().astimezone().isoformat(), "event": event}
        row.update({k: v for k, v in fields.items() if k in allowed})
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")


@contextmanager
def single_instance(path):
    """OS lock survives neither crashes nor process exit; keep inode in place."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise StopRun("이 프로젝트의 다른 조회·예약 작업이 실행 중입니다.") from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class Budget:
    def __init__(self, policy, audit, clock=time.monotonic, sleep=time.sleep):
        self.policy, self.audit, self.clock, self.sleep = policy, audit, clock, sleep
        self.deadline = clock() + policy.max_seconds if policy.max_seconds is not None else math.inf
        self.requests = 0
        self.stopped = None

    def check(self):
        if self.stopped:
            raise StopRun(self.stopped)
        if self.clock() >= self.deadline:
            raise StopRun("설정한 실행 시간이 끝났습니다.")

    def wait(self, seconds):
        self.check()
        self.sleep(min(seconds, max(0, self.deadline - self.clock())))
        self.check()

    def attach(self, client):
        sessions = [client._session]
        helper = getattr(client, "_netfunnel", None)
        if helper is not None:
            sessions.append(helper._session)
        seen = set()
        for session in sessions:
            if id(session) not in seen:
                self.wrap(session, httpx=getattr(client, "uses_httpx", False) is True)
                seen.add(id(session))

    def wrap(self, session, httpx=False):
        original = session.request

        def request(*args, **kwargs):
            self.check()
            if self.policy.max_requests is not None and self.requests >= self.policy.max_requests:
                raise StopRun("전체 HTTP 요청 한도에 도달했습니다.")
            self.requests += 1
            # Disable hidden redirect requests and bound each transport call.
            kwargs["follow_redirects" if httpx else "allow_redirects"] = False
            kwargs["timeout"] = min(15, self.deadline - self.clock())
            started = self.clock()
            try:
                response = original(*args, **kwargs)
            except Exception as exc:
                self.audit("http_error", count=self.requests,
                           error_type=type(exc).__name__, elapsed=self.clock() - started)
                raise
            self.audit("http", count=self.requests, status=response.status_code,
                       elapsed=self.clock() - started)
            if response.status_code in (401, 403, 429) or 300 <= response.status_code < 400:
                self.stopped = "인증·접근 제한 또는 리디렉션 응답입니다. 공식 앱에서 확인하세요."
                raise StopRun(self.stopped)
            response.raise_for_status()
            return response

        session.request = request


def attribute(obj, *names):
    for name in names:
        value = getattr(obj, name, None)
        if value is not None:
            return str(value)
    raise ValueError("열차 응답의 필수 필드가 없습니다.")


def station_name(value):
    return value.strip().replace("김천(구미)", "김천구미")


def train_key(train):
    return (attribute(train, "dep_date"),
            attribute(train, "train_no", "train_number").lstrip("0") or "0",
            station_name(attribute(train, "dep_name", "dep_station_name")),
            station_name(attribute(train, "arr_name", "arr_station_name")),
            attribute(train, "dep_time"))


def matches(train, query):
    date, number, departure, arrival, departure_time = train_key(train)
    return (date == query.date and departure == station_name(query.departure)
            and arrival == station_name(query.arrival)
            and query.start <= departure_time <= query.end
            and (not query.train_number or number == (query.train_number.lstrip("0") or "0")))


class AttemptStore:
    """Persist reservation outcomes to prevent repeating recorded attempts."""

    def __init__(self, path):
        self.path = path
        self.states = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(self.states, dict):
            raise ValueError("예약 상태 파일을 읽을 수 없습니다.")

    def key(self, train):
        return hashlib.sha256(json.dumps(train_key(train)).encode()).hexdigest()

    def contains(self, train):
        return self.states.get(self.key(train), {}).get("state") not in (None, "retryable")

    def state(self, train):
        return self.states.get(self.key(train), {}).get("state")

    def record(self, train, state):
        self.states[self.key(train)] = {"state": state, "time": datetime.now().isoformat()}
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.states), encoding="utf-8")
        temporary.replace(self.path)


class Adapter:
    def __init__(self, client, rail, query=None, audit=None):
        if rail != "KTX":
            raise ValueError("KTX만 지원합니다.")
        self.client, self.query = client, query
        self.audit = audit

    def search(self, query):
        # Return usable seats as soon as a page contains matching trains.
        for trains in self.client.search_pages(query):
            if any(self.available(train) for train in trains):
                return trains
        return []

    def available(self, train):
        general = train.has_general_seat
        special = train.has_special_seat
        seat = self.query.seat if self.query else "general"
        if seat == "general":
            return general()
        if seat == "special":
            return special()
        return general() or special()

    def reservations(self):
        return self.client.reservations()

    def reserve(self, train):
        return self.client.reserve(train, self.query)

    def login(self):
        try:
            with redirect_stdout(io.StringIO()):
                result = self.client.login()
            authenticated = self.client.logined
            if result is False or not authenticated:
                raise LoginFailure("server_rejected")
        except LoginFailure as exc:
            if self.audit:
                self.audit("login", state=exc.reason)
            raise
        else:
            if self.audit:
                self.audit("login", state="authenticated")


def is_restriction(exc):
    # Raw messages are inspected locally but never printed or written to logs.
    message = str(exc).lower()
    return isinstance(exc, StopRun) or type(exc).__name__ in (
        "KorailDynaPathError", "KorailDynaPathRequiredError", "KorailQueueRejectedError",
        "KorailAuthContinuationRequired",
    ) or any(word in message for word in (
        "macro", "매크로", "비정상", "정상적인 경로", "captcha", "차단", "접근 제한"))


def error_kind(exc):
    if is_restriction(exc):
        return "stop"
    if type(exc).__name__ in ("KorailSessionExpiredError",):
        return "session"
    if type(exc).__name__ in ("NoResultsError", "KorailNoResultsError", "KorailNoDirectTrainError"):
        return "empty"
    names = {cls.__name__ for cls in type(exc).__mro__}
    module = type(exc).__module__
    if type(exc).__name__ == "KorailTransportError" and exc.__cause__ is not None:
        return error_kind(exc.__cause__)
    if isinstance(exc, (TimeoutError, ConnectionError)) or (
            module.startswith(("requests.", "curl_cffi.", "httpx"))
            and names.intersection({"Timeout", "ConnectionError", "TimeoutException", "NetworkError"})):
        return "transient"
    return "stop"


def retry_read_operation(operation, budget, label, emit=print):
    """Retry login/read preparation only; never replay a reservation mutation."""
    for attempt in range(1, budget.policy.max_errors + 1):
        budget.check()
        try:
            result = operation()
        except Exception as exc:
            kind = error_kind(exc)
            cause = exc.__cause__ if exc.__cause__ is not None else exc
            budget.audit("prepare_error", operation=label, state=kind,
                         count=attempt, error_type=type(cause).__name__)
            if kind != "transient":
                raise
            if attempt >= budget.policy.max_errors:
                raise StopRun(f"{label}: 연속 통신 오류 {attempt}회로 중단했습니다 "
                              f"({type(cause).__name__}). 잠시 후 다시 실행하세요.") from exc
            delay = max(budget.policy.interval,
                        min(max(60, budget.policy.interval) * 2 ** attempt, 300))
            emit(f"{label}: 통신 오류 ({type(cause).__name__}). "
                 f"{delay:g}초 후 재시도합니다 ({attempt}/{budget.policy.max_errors}).")
            budget.wait(delay)
        else:
            return result


def notify_reserved(reservation, notifier, audit, emit):
    if notifier is None:
        return
    try:
        notifier(reservation)
    except Exception as exc:
        audit("telegram", state="failed", error_type=type(exc).__name__)
        emit("예약은 완료됐지만 텔레그램 알림 전송에 실패했습니다. 공식 앱에서 구입기한을 확인하세요.")
    else:
        audit("telegram", state="sent")
        emit("텔레그램으로 예약 성공 알림을 보냈습니다.")


def reconcile_reservation(adapter, train, budget, store, intervals, emit):
    """Require two complete empty history reads, separated by a normal wait."""
    absent = 0
    while True:
        budget.check()
        try:
            found = next((r for r in adapter.reservations()
                          if train_key(r) == train_key(train)), None)
        except Exception as exc:
            budget.audit("reconcile_error", error_type=type(exc).__name__)
            if is_restriction(exc):
                raise
            absent = 0
            emit(f"예약 내역 확인 오류 ({type(exc).__name__}). 대기 후 다시 확인합니다.")
        else:
            if found is not None:
                store.record(train, "reserved")
                return found
            absent += 1
            budget.audit("reconcile", state="absent", count=absent)
            if absent >= 2:
                store.record(train, "retryable")
                emit("예약·승차권 내역에서 해당 열차가 두 차례 확인되지 않았습니다. 잔여석 조회와 예약을 재시도합니다.")
                return None
        delay = intervals.next_delay()
        emit(f"예약 내역 재확인까지 {delay:.1f}초 대기")
        budget.wait(delay)


def watch(adapter, query, budget, store, emit=print, notifier=None):
    errors, renewed = 0, False
    intervals = IntervalQueue(budget.policy)
    # Fetch duplicates before polling, away from the seat-discovery hot path.
    budget.check()
    existing_keys = ({train_key(r) for r in retry_read_operation(
                        adapter.reservations, budget, "기존 예약·승차권 확인", emit)}
                     if query.reserve else set())
    attempt = 0
    while budget.policy.max_searches is None or attempt < budget.policy.max_searches:
        budget.check()
        attempt += 1
        started = budget.clock()
        delay = None
        try:
            trains = adapter.search(query)
        except Exception as exc:
            kind = error_kind(exc)
            budget.audit("search_error", error_type=type(exc).__name__, state=kind,
                         count=attempt, elapsed=budget.clock() - started)
            if kind == "session" and not renewed:
                renewed = True
                retry_read_operation(adapter.login, budget, "세션 재로그인", emit)
            elif kind == "transient":
                errors += 1
                if errors >= budget.policy.max_errors:
                    raise StopRun("연속 통신 오류 한도에 도달했습니다.") from exc
                # Keep error backoff separate from the shorter normal waits.
                delay = max(budget.policy.interval,
                            min(max(60, budget.policy.interval) * 2 ** errors, 300))
            elif kind != "empty":
                raise StopRun("조회가 중단됐습니다. 오류 종류는 로그에서 확인하세요.") from exc
        else:
            errors = 0
            candidates = [train for train in trains if matches(train, query)]
            for train in candidates:
                if not adapter.available(train):
                    continue
                if not query.reserve:
                    budget.audit("search", count=attempt, elapsed=budget.clock() - started)
                    emit(f"선택한 좌석 조건의 잔여석 발견: {train}")
                    emit("알림 모드입니다. 공식 앱에서 예약하세요.")
                    return "available"
                if store.contains(train):
                    if store.state(train) != "uncertain":
                        raise StopRun("이 열차의 이전 예약 시도 기록이 있습니다. 공식 앱에서 내역을 확인하세요.")
                    found = reconcile_reservation(adapter, train, budget, store, intervals, emit)
                    if found is not None:
                        emit(f"예약 내역에서 확인했습니다: {found}")
                        emit("결제는 공식 앱에서 구입기한 내에 완료하세요.")
                        notify_reserved(found, notifier, budget.audit, emit)
                        return "reserved"
                    # Refresh availability after the history checks and wait.
                    break
                budget.check()
                if train_key(train) in existing_keys:
                    store.record(train, "existing")
                    emit("같은 열차의 예약·승차권이 이미 있습니다. 추가 예약 없이 종료합니다.")
                    return "existing"
                budget.check()
                try:
                    reserved = adapter.reserve(train)
                    if reserved is None or reserved is False:
                        raise ValueError("Missing reservation result")
                except BaseException as exc:
                    # Write only after the call, including manual interruption.
                    store.record(train, "uncertain")
                    budget.audit("search", count=attempt, elapsed=budget.clock() - started)
                    trace = exc.__traceback__
                    while trace is not None and trace.tb_next is not None:
                        trace = trace.tb_next
                    # Code location only: never log exception text or frame locals.
                    location = (f"{Path(trace.tb_frame.f_code.co_filename).name}:"
                                f"{trace.tb_lineno}" if trace is not None else "unknown")
                    budget.audit("reservation_error", error_type=type(exc).__name__,
                                 operation=location)
                    emit(f"예약 완료 확인 실패 ({type(exc).__name__}): {train}")
                    if not isinstance(exc, Exception):
                        raise
                    if is_restriction(exc):
                        raise StopRun("예약 중 실행 한도 또는 접근 제한이 확인됐습니다.") from exc
                    found = reconcile_reservation(adapter, train, budget, store, intervals, emit)
                    if found is not None:
                        emit(f"예약 내역에서 확인했습니다: {found}")
                        emit("결제는 공식 앱에서 구입기한 내에 완료하세요.")
                        notify_reserved(found, notifier, budget.audit, emit)
                        return "reserved"
                    break
                store.record(train, "reserved")
                budget.audit("search", count=attempt, elapsed=budget.clock() - started)
                budget.audit("reservation", state="reserved")
                emit(f"예약 완료: {reserved}")
                emit("결제는 공식 앱에서 구입기한 내에 완료하세요.")
                notify_reserved(reserved, notifier, budget.audit, emit)
                return "reserved"
            else:
                budget.audit("search", count=attempt, elapsed=budget.clock() - started)
                progress = f"{attempt}/{budget.policy.max_searches}" if budget.policy.max_searches is not None else str(attempt)
                emit(f"{progress}회 조회: 조건에 맞는 잔여석 없음")
        if budget.policy.max_searches is None or attempt < budget.policy.max_searches:
            if delay is None:
                delay = intervals.next_delay()
            emit(f"다음 조회까지 {delay:.1f}초 대기")
            budget.wait(delay)
    return "limit"


def credentials(rail="KTX"):
    if rail != "KTX":
        raise ValueError("KTX만 지원합니다.")
    import keyring
    user = os.environ.get(f"{rail}_ID") or keyring.get_password(rail, "id")
    password = os.environ.get(f"{rail}_PASSWORD") or keyring.get_password(rail, "pass")
    return user or input(f"{rail} 회원 ID: ").strip(), password or getpass(f"{rail} 비밀번호: ")


def create_client(rail, user, password):
    if rail != "KTX":
        raise ValueError("KTX만 지원합니다.")
    from korail_adapter import MobileKorail
    return MobileKorail(user, password)


def close_client(client):
    client.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="성공 또는 직접 중단까지 잔여석 조회 및 시간대별 열차 예약")
    parser.add_argument("--dep")
    parser.add_argument("--arr")
    parser.add_argument("--date")
    parser.add_argument("--time", default="000000")
    parser.add_argument("--until", default="235959")
    parser.add_argument("--train-number", default="", help="대상 열차 번호 (생략하면 시간대 내 모든 열차)")
    parser.add_argument("--reserve", action="store_true", help="조건에 맞는 열차 한 편 예약, 결제 제외")
    parser.add_argument("--adults", type=int, default=1)
    parser.add_argument("--children", type=int, default=0)
    parser.add_argument("--seniors", type=int, default=0)
    parser.add_argument("--disability1to3", type=int, default=0)
    parser.add_argument("--disability4to6", type=int, default=0)
    parser.add_argument("--seat", choices=("general", "special", "general-first", "special-first"), default="general")
    parser.add_argument("--interval", type=float, default=Policy.interval, help="최소 조회 대기 시간 (15초 이상)")
    parser.add_argument("--max-interval", type=float, default=Policy.max_interval, help="최대 조회 대기 시간")
    parser.add_argument("--max-searches", type=int, default=Policy.max_searches, help="조회 횟수 상한 (생략 시 무제한)")
    parser.add_argument("--max-minutes", type=float, default=None, help="실행 시간 상한, 분 (생략 시 무제한)")
    parser.add_argument("--max-requests", type=int, default=Policy.max_requests, help="HTTP 요청 상한 (생략 시 무제한)")
    args = parser.parse_args(argv)
    try:
        policy = Policy(args.interval, args.max_searches,
                        args.max_minutes * 60 if args.max_minutes is not None else None, args.max_requests,
                        max_interval=args.max_interval)
        policy.validate()
        query = Query("KTX", args.dep or input("출발역: ").strip(),
                      args.arr or input("도착역: ").strip(),
                      args.date or input("운행일 YYYYMMDD: ").strip(),
                      args.time, args.until, args.train_number, args.reserve,
                      args.adults, args.children, args.seniors, args.disability1to3, args.disability4to6, args.seat)
        query.validate()
        with single_instance(RUNTIME / "reservation.lock"):
            audit = Audit(RUNTIME / "events.jsonl")
            store = AttemptStore(RUNTIME / "attempts.json")
            user, password = credentials(query.rail)
            client = create_client(query.rail, user, password)
            budget = Budget(policy, audit)
            budget.attach(client)
            adapter = Adapter(client, query.rail, query, audit=audit)
            audit("run", state="reserve" if query.reserve else "notify")
            print(f"{'자동예약' if query.reserve else '잔여석 알림'} / "
                  f"{policy.limit_summary()} (직접 중단: Ctrl+C)")
            print(f"조회 간격: {policy.interval:g}~{policy.max_interval:g}초 / 10개 대기열 균형 조정")
            print(f"대상: {query.departure} → {query.arrival} / {query.date} / "
                  f"{query.start}~{query.end} / "
                  f"{'열차 ' + query.train_number if query.train_number else '시간대 내 모든 열차'}")
            try:
                notifier = None
                if query.reserve:
                    from telegram_notifications import load_notifier
                    notifier = load_notifier()
                retry_read_operation(adapter.login, budget, "로그인")
                result = watch(adapter, query, budget, store, notifier=notifier)
                audit("finished", state=result)
                print("작업을 종료했습니다.")
                return 0
            except Exception as exc:
                audit("finished", state="stopped", error_type=type(exc).__name__)
                raise
            finally:
                close_client(client)
    except (StopRun, ValueError) as exc:
        # These errors are generated locally; server exceptions stay private.
        if isinstance(exc, StopRun):
            print(f"중단: {exc}")
        else:
            print("입력 또는 로컬 상태를 확인하세요. 날짜·시각·열차 번호와 제한값이 유효해야 합니다.")
        return 2
    except (KeyboardInterrupt, EOFError):
        print("작업을 중단했습니다. 예약 시도 후라면 공식 앱에서 예약 내역을 확인하세요.")
        return 130
    except Exception as exc:
        print(f"작업 중단 ({type(exc).__name__}). 인증·통신 상태를 공식 앱에서 확인하세요.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
