"""Project interface for the pinned korail-mobile-api client.

Only explicit reserve mode enables an unpaid hold. No payment/cancel interface.
All requests use the one httpx client exposed to reservation_guard.Budget.
"""

from dataclasses import dataclass, field, replace
from types import SimpleNamespace

from korail_mobile_api import (
    KorailClient, KorailConfig, KorailPassengerCounts, KorailSeatClass,
    MutationConsent, TrainSearchQuery,
)
from korail_mobile_api.errors import (
    KorailAuthError, KorailAuthContinuationRequired, KorailDynaPathError,
    KorailNoResultsError, KorailProtocolError,
)

from reservation_guard import LoginFailure, StopRun, matches, train_key


@dataclass(frozen=True)
class TrainView:
    source: object = field(repr=False)

    @property
    def dep_date(self):
        return self.source.departure_date

    @property
    def train_no(self):
        return self.source.train_no

    @property
    def dep_name(self):
        return self.source.departure_station_name

    @property
    def arr_name(self):
        return self.source.arrival_station_name

    @property
    def dep_time(self):
        return self.source.departure_time

    def has_general_seat(self):
        return getattr(self.source, "general_reservation_code", None) == "11"

    def has_special_seat(self):
        return getattr(self.source, "special_reservation_code", None) == "11"

    def __str__(self):
        return (f"열차 {self.train_no} / {self.dep_date} / "
                f"{self.dep_name} → {self.arr_name} / {self.dep_time}")


class MobileKorail:
    modern_api = True
    uses_httpx = True

    def __init__(self, user, password, *, transport=None):
        self._user, self._password = user, password
        self.api = KorailClient(KorailConfig(enable_dynapath=True, timeout=15), transport=transport)
        self._session = self.api.http._client

    @property
    def logined(self):
        return self.api.session.current is not None

    def login(self):
        try:
            self.api.login(self._user, self._password)
        except KorailAuthContinuationRequired:
            raise LoginFailure("continuation_required") from None
        except KorailDynaPathError:
            raise LoginFailure("macro_blocked") from None
        except KorailAuthError as exc:
            cause = exc.__cause__
            reason = "macro_blocked" if getattr(cause, "code", None) == "MACRO ERROR" else "server_rejected"
            raise LoginFailure(reason) from None
        except KorailProtocolError:
            raise LoginFailure("invalid_response") from None
        return self.logined

    def search(self, query):
        return sorted(
            (train for trains in self.search_pages(query) for train in trains),
            key=lambda t: (t.dep_time, t.train_no),
        )

    def search_pages(self, query):
        """Yield each validated page without requesting the following page early."""
        passengers = self.passengers(query)
        request = TrainSearchQuery(
            query.departure, query.arrival, query.date, query.start,
            passengers=passengers.total, train_group_code="100",
        )
        continuation, seen_cursors, seen_trains = None, set(), set()
        while True:
            try:
                result = self.api.search_trains(request, continuation=continuation)
            except KorailNoResultsError:
                break
            times = [t.departure_time for t in result.trains]
            if times != sorted(times):
                raise StopRun("열차 목록의 시간 순서를 확인할 수 없습니다.")
            found = []
            for source in result.trains:
                train = TrainView(source)
                key = train_key(train)
                if matches(train, query) and key not in seen_trains:
                    seen_trains.add(key)
                    found.append(train)
            yield sorted(found, key=lambda t: (t.dep_time, t.train_no))
            if times and times[-1] > query.end:
                break
            continuation = result.next_page()
            if continuation is None:
                if result.metadata.next_page_flag == "Y":
                    # Current server returns Y without opaque cursor fields.
                    # Include the final timestamp again so tied departures survive.
                    if not times or times[-1] <= request.departure_time:
                        raise StopRun("열차 목록의 다음 페이지가 진행되지 않아 중단했습니다.")
                    request = replace(request, departure_time=times[-1])
                    continue
                else:
                    break
            if continuation in seen_cursors:
                raise StopRun("열차 목록의 다음 페이지가 반복돼 중단했습니다.")
            seen_cursors.add(continuation)

    @staticmethod
    def passengers(query):
        return KorailPassengerCounts(
            adult=query.adults, child=query.children, senior=query.seniors,
            severe_disability=query.disability1to3, mild_disability=query.disability4to6,
        )

    def reserve(self, train, query):
        if query is None or not query.reserve or not matches(train, query):
            raise StopRun("자동예약 조건과 대상 열차가 일치하지 않습니다.")
        order = {
            "general": (KorailSeatClass.GENERAL,),
            "special": (KorailSeatClass.SPECIAL,),
            "general-first": (KorailSeatClass.GENERAL, KorailSeatClass.SPECIAL),
            "special-first": (KorailSeatClass.SPECIAL, KorailSeatClass.GENERAL),
        }[query.seat]
        seat = next((seat for seat in order if (
            train.has_general_seat() if seat == KorailSeatClass.GENERAL else train.has_special_seat()
        )), None)
        if seat is None:
            raise StopRun("선택한 좌석 조건의 잔여석이 없습니다.")
        hold = self.api.reserve(
            train.source, passengers=self.passengers(query), seat_class=seat,
            consent=MutationConsent(allow_reserve=True, dry_run=False),
        )
        if not hold.pnr_no:
            raise ValueError("Missing reservation confirmation")
        # Never print the raw hold (PNR, payment metadata and account fields).
        return train

    def reservations(self):
        history = self.api.get_reservation_history()
        found = [self._existing(row.raw) for row in history.items]
        page, seen_pages, received_rows = 1, set(), 0
        while True:
            try:
                payload = self.api.get_ticket_list(page_no=page).raw
            except KorailNoResultsError:
                break
            rows = payload.get("reservation_list", payload.get("tickets"))
            if rows is None and payload.get("h_msg_cd") == "WRT300005":
                break
            if not isinstance(rows, list):
                raise StopRun("승차권 목록 형식을 확인할 수 없어 중복 예약 검사를 중단했습니다.")
            if not rows:
                break
            trains = [train for row in rows for train in self._ticket_trains(row)]
            signature = tuple(train_key(t) for t in trains)
            if signature in seen_pages:
                raise StopRun("승차권 페이지가 반복돼 중복 예약 검사를 중단했습니다.")
            seen_pages.add(signature)
            found.extend(trains)
            received_rows += len(rows)
            # Active-ticket responses report the list's row count and may repeat
            # the same list even when h_page_no is incremented beyond its end.
            total = payload.get("h_row_cnt")
            if total is not None:
                if not isinstance(total, (str, int)) or isinstance(total, bool) or not str(total).isdigit():
                    raise StopRun("승차권 목록의 전체 건수를 확인할 수 없습니다.")
                if received_rows >= int(total):
                    if payload.get("h_next_pg_flg") == "Y":
                        raise StopRun("승차권 목록의 건수와 다음 페이지 표시가 일치하지 않습니다.")
                    break
            if payload.get("h_next_pg_flg") == "N":
                break
            # Without completion metadata, require an explicitly empty page.
            page += 1
        return found

    @classmethod
    def _ticket_trains(cls, row):
        if not isinstance(row, dict):
            raise StopRun("승차권의 열차 정보를 확인할 수 없습니다.")
        if "ticket_list" not in row:
            return [cls._existing(row)]
        tickets = row["ticket_list"]
        if not isinstance(tickets, list) or not tickets:
            raise StopRun("승차권의 상세 목록을 확인할 수 없습니다.")
        trains = []
        for ticket in tickets:
            rows = ticket.get("train_info") if isinstance(ticket, dict) else None
            if not isinstance(rows, list) or not rows:
                raise StopRun("승차권의 열차 목록을 확인할 수 없습니다.")
            trains.extend(cls._existing(row) for row in rows)
        return trains

    @staticmethod
    def _existing(row):
        if not isinstance(row, dict):
            raise StopRun("기존 예약의 열차 정보를 확인할 수 없습니다.")
        fields = dict(
            departure_date=row.get("h_dpt_dt") or row.get("h_run_dt"),
            departure_time=row.get("h_dpt_tm"), train_no=row.get("h_trn_no"),
            departure_station_name=row.get("h_dpt_rs_stn_nm"),
            arrival_station_name=row.get("h_arv_rs_stn_nm"),
        )
        if not all(isinstance(v, str) and v for v in fields.values()):
            raise StopRun("기존 예약의 열차 정보를 확인할 수 없어 중복 예약 검사를 중단했습니다.")
        return TrainView(SimpleNamespace(**fields))

    def close(self):
        self.api.clear_session()
        self.api.close()
