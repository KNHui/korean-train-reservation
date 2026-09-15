import contextlib
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs

import httpx
from korail_mobile_api.errors import KorailSessionExpiredError, KorailTransportError

from korail_adapter import MobileKorail
from reservation_guard import (
    Adapter, AttemptStore, Audit, Budget, LoginFailure, Policy, Query, StopRun,
    error_kind, retry_read_operation, train_key, watch,
)


BASE = {"strResult": "SUCC", "h_msg_cd": "IRZ000001", "h_msg_txt": "success"}


def row(number="001", time="120000", general="11", special="13"):
    return {
        "h_trn_no": number, "h_trn_gp_cd": "100", "h_trn_clsf_cd": "00",
        "h_dpt_dt": "20990101", "h_run_dt": "20990101", "h_dpt_tm": time,
        "h_arv_tm": "140000", "h_dpt_rs_stn_cd": "0551", "h_arv_rs_stn_cd": "0015",
        "h_dpt_rs_stn_nm": "수서", "h_arv_rs_stn_nm": "동대구",
        "h_dpt_stn_cons_ordr": "000001", "h_arv_stn_cons_ordr": "000005",
        "h_dpt_stn_run_ordr": "000001", "h_arv_stn_run_ordr": "000005",
        "h_gen_rsv_cd": general, "h_spe_rsv_cd": special,
    }


def page(rows, more="N", **metadata):
    return {**BASE, "trn_infos": {"trn_info": rows}, "h_next_pg_flg": more, **metadata}


class MobileKorailTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.calls = []
        self.pages = [page([row()])]
        self.login_reply = {**BASE, "strMbCrdNo": "private-member"}
        self.cookie = True
        self.history = {**BASE, "jrny_infos": {"jrny_info": []}}
        self.tickets = [{**BASE, "tickets": []}]
        self.reserve_reply = {**BASE, "h_pnr_no": "1234567890", "h_jrny_cnt": "0001"}
        self.client = MobileKorail("private-id", "private-password", transport=httpx.MockTransport(self.handle))
        self.addCleanup(self.client.close)
        self.audit = Audit(self.root / "events.jsonl")
        self.budget = Budget(Policy(), self.audit)
        self.budget.attach(self.client)
        self.query = Query("KTX", "수서", "동대구", "20990101", "120000", "235959", reserve=True)
        self.adapter = Adapter(self.client, "KTX", self.query, audit=self.audit)

    def handle(self, request):
        path = request.url.path
        body = parse_qs(request.content.decode(), keep_blank_values=True)
        self.calls.append((path, body, request))
        headers = {}
        if path.endswith("MobileService.cache"):
            payload = BASE
        elif path.endswith("common.code.do"):
            payload = {**BASE, "app.login.cphd": {"idx": "1", "key": "0123456789abcdef", "pwdAESCphd": "Y"}}
        elif path.endswith("login.Login"):
            payload = self.login_reply
            if self.cookie:
                headers["set-cookie"] = "JSESSIONID=private-session; Path=/"
        elif path.endswith("seatMovie.ScheduleView"):
            payload = self.pages.pop(0)
        elif path.endswith("reservation.ReservationView"):
            payload = self.history
        elif path.endswith("myTicket.MyTicketList"):
            payload = self.tickets.pop(0)
        elif path.endswith("certification.TicketReservation"):
            payload = self.reserve_reply
        else:
            self.fail("Unexpected network route")
        return httpx.Response(200, json=payload, headers=headers)

    def login(self):
        self.adapter.login()

    def test_login_uses_new_auth_and_counts_every_request_without_leaking(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.login()
        self.assertTrue(self.client.logined)
        self.assertEqual(self.budget.requests, 3)
        request = self.calls[-1][2]
        self.assertTrue(request.headers.get("x-dynapath-m-token"))
        self.assertNotIn("private-password", request.content.decode())
        self.assertLessEqual(request.extensions["timeout"]["read"], 15)
        self.assertIn("authenticated", self.audit.path.read_text())
        self.assertNotIn("private-", output.getvalue() + self.audit.path.read_text())

    def test_macro_rejection_and_success_without_cookie_both_stop(self):
        self.login_reply = {"strResult": "FAIL", "h_msg_cd": "MACRO ERROR", "h_msg_txt": "private-message"}
        with self.assertRaises(LoginFailure) as caught:
            self.login()
        self.assertEqual(caught.exception.reason, "macro_blocked")
        self.assertEqual(len(self.calls), 3)
        self.assertFalse(self.client.logined)
        self.login_reply, self.cookie = BASE, False
        with self.assertRaises(LoginFailure):
            self.login()
        self.assertFalse(self.client.logined)

    def test_wrapped_login_connect_timeout_recovers_with_same_client(self):
        transport = self.client._session._transport
        original = transport.handle_request
        failed = False

        def handle(request):
            nonlocal failed
            if request.url.path.endswith("login.Login") and not failed:
                failed = True
                raise httpx.ConnectTimeout("private-endpoint", request=request)
            return original(request)

        self.budget.sleep = Mock()
        emit = Mock()
        with patch.object(transport, "handle_request", side_effect=handle):
            retry_read_operation(self.adapter.login, self.budget, "로그인", emit)
        self.assertTrue(self.client.logined)
        self.budget.sleep.assert_called_once_with(120)
        self.assertIn("ConnectTimeout", str(emit.call_args_list))
        self.assertNotIn("private-endpoint", str(emit.call_args_list)
                         + self.audit.path.read_text(encoding="utf-8"))
    def test_cursorless_paging_keeps_tied_departures_and_removes_overlap(self):
        self.pages = [page([row("1"), row("2", "130000")], "Y"),
                      page([row("2", "130000"), row("3", "130000"), row("4", "140000")])]
        self.login()
        result = self.client.search(self.query)
        self.assertEqual([t.train_no for t in result], ["1", "2", "3", "4"])
        self.assertEqual(self.calls[-1][1]["txtGoHour"], ["130000"])
        self.assertEqual(self.budget.requests, 5)

    def test_opaque_cursor_and_search_passenger_count_are_forwarded(self):
        self.pages = [page([row("1")], "Y", h_qry_st_no_next="0001", h_trn_no_next="00002", h_rslt_cnt="10"), page([row("2", "130000")])]
        self.login()
        result = self.client.search(replace(self.query, adults=2, children=1))
        self.assertEqual(len(result), 2)
        form = self.calls[-1][1]
        self.assertEqual(form["qryStTrnNo"], ["00002"])
        self.assertEqual(form["txtPsgFlg_1"], ["3"])

    def test_repeated_time_boundary_stops_instead_of_looping(self):
        self.pages = [page([row()], "Y")]
        self.login()
        with self.assertRaises(StopRun):
            self.client.search(self.query)
        self.assertEqual(self.budget.requests, 4)

    def test_paid_and_unpaid_duplicates_are_read_and_paid_pages_are_complete(self):
        self.history = {**BASE, "jrny_infos": {"jrny_info": [{"train_infos": {"train_info": [row("1")]}}]}}
        self.tickets = [{**BASE, "reservation_list": [row("2")]}, {**BASE, "reservation_list": [row("3")]}, {**BASE, "reservation_list": []}]
        self.login()
        existing = self.adapter.reservations()
        self.assertEqual([t.train_no for t in existing], ["1", "2", "3"])
        self.assertEqual([call[1]["h_page_no"] for call in self.calls if call[0].endswith("MyTicketList")], [["1"], ["2"], ["3"]])

    def test_first_page_seat_is_reserved_without_loading_second_page(self):
        self.pages = [page([row("1"), row("2", "130000")], "Y"),
                      page([row("3", "140000")])]
        self.login()
        result = watch(self.adapter, self.query, self.budget,
                       AttemptStore(self.root / "attempts.json"), emit=Mock())
        self.assertEqual(result, "reserved")
        self.assertEqual(len(self.pages), 1)
        paths = [path for path, _, _ in self.calls]
        self.assertTrue(paths[-2].endswith("ScheduleView"))
        self.assertTrue(paths[-1].endswith("TicketReservation"))
        self.assertEqual(sum(path.endswith("TicketReservation") for path in paths), 1)

    def test_sold_out_page_continues_to_next_page_without_poll_wait(self):
        self.pages = [page([row("1", "130000", general="13")], "Y"),
                      page([row("1", "130000", general="13"), row("2", "140000")])]
        self.budget.sleep = Mock(side_effect=AssertionError("Unexpected polling wait"))
        self.login()
        result = watch(self.adapter, self.query, self.budget,
                       AttemptStore(self.root / "attempts.json"), emit=Mock())
        self.assertEqual(result, "reserved")
        self.assertEqual(len(self.pages), 0)
        self.assertEqual(self.calls[-1][1]["txtTrnNo1"], ["2"])

    def test_paging_respects_train_and_seat_filters(self):
        self.query = replace(self.query, train_number="3", seat="special")
        self.adapter.query = self.query
        self.pages = [page([row("1", "130000", special="11")], "Y"),
                      page([row("2", "140000", special="11"),
                            row("3", "150000", special="11")])]
        self.login()
        result = watch(self.adapter, self.query, self.budget,
                       AttemptStore(self.root / "attempts.json"), emit=Mock())
        self.assertEqual(result, "reserved")
        self.assertEqual(self.calls[-1][1]["txtTrnNo1"], ["3"])
        self.assertEqual(self.calls[-1][1]["txtPsrmClCd1"], ["2"])

    def test_invalid_page_order_stops_before_reservation(self):
        self.pages = [page([row("1", "140000"), row("2", "130000")])]
        self.login()
        with self.assertRaises(StopRun):
            watch(self.adapter, self.query, self.budget,
                  AttemptStore(self.root / "attempts.json"), emit=Mock())
        self.assertFalse(any(path.endswith("TicketReservation") for path, _, _ in self.calls))

    def test_later_page_transport_error_uses_search_retry_policy(self):
        self.pages = [page([row("1", "130000", general="13")], "Y")]
        self.login()
        original = self.client.api.search_trains
        self.budget.policy = Policy(max_searches=2)
        self.budget.sleep = Mock()

        def search(*args, **kwargs):
            if self.pages:
                return original(*args, **kwargs)
            raise httpx.ReadTimeout("offline")

        with patch.object(self.client.api, "search_trains", side_effect=search):
            result = watch(self.adapter, self.query, self.budget,
                           AttemptStore(self.root / "attempts.json"), emit=Mock())
        self.assertEqual(result, "limit")
        self.budget.sleep.assert_called_once_with(120)
        self.assertFalse(any(path.endswith("TicketReservation") for path, _, _ in self.calls))

    def test_unknown_ticket_shape_stops_before_reservation(self):
        self.tickets = [{**BASE, "unknown": []}]
        self.login()
        with self.assertRaises(StopRun):
            watch(self.adapter, self.query, self.budget, AttemptStore(self.root / "attempts.json"), emit=Mock())
        self.assertFalse(any(p.endswith("TicketReservation") for p, _, _ in self.calls))
        self.assertFalse((self.root / "attempts.json").exists())

    def test_nested_paid_tickets_include_every_ticket_and_train_leg(self):
        self.tickets = [{**BASE, "reservation_list": [{"ticket_list": [
            {"train_info": [row("2"), row("3")]}, {"train_info": [row("4")]},
        ]}]}, {**BASE, "reservation_list": []}]
        self.login()
        self.assertEqual([t.train_no for t in self.adapter.reservations()], ["2", "3", "4"])

    def test_nested_paid_duplicate_prevents_a_new_hold(self):
        self.tickets = [{**BASE, "reservation_list": [{"ticket_list": [{"train_info": [row()]}]}]},
                        {**BASE, "reservation_list": []}]
        self.login()
        result = watch(self.adapter, self.query, self.budget, AttemptStore(self.root / "attempts.json"), emit=Mock())
        self.assertEqual(result, "existing")
        self.assertFalse(any(p.endswith("TicketReservation") for p, _, _ in self.calls))

    def test_repeated_paid_page_stops_before_reserving(self):
        payload = {**BASE, "reservation_list": [row()]}
        self.tickets = [payload, payload]
        self.login()
        with self.assertRaises(StopRun):
            self.adapter.reservations()

    def test_active_ticket_row_count_finishes_without_requesting_repeated_page(self):
        self.tickets = [{**BASE, "h_row_cnt": "1", "reservation_list": [
            {"ticket_list": [{"train_info": [row("2"), row("3")]}]},
        ]}]
        self.login()
        self.assertEqual([t.train_no for t in self.adapter.reservations()], ["2", "3"])
        self.assertEqual(len(self.calls), 5)

    def test_total_ticket_count_reads_later_pages(self):
        self.tickets = [{**BASE, "h_row_cnt": "2", "reservation_list": [row("2")]},
                        {**BASE, "h_row_cnt": "2", "reservation_list": [row("3")]}]
        self.login()
        self.assertEqual([t.train_no for t in self.adapter.reservations()], ["2", "3"])

    def test_explicit_empty_ticket_code_needs_no_list_field(self):
        self.tickets = [{**BASE, "h_msg_cd": "WRT300005"}]
        self.login()
        self.assertEqual(self.adapter.reservations(), [])

    def test_real_request_building_reserves_exactly_once_with_passengers_and_special_seat(self):
        self.query = replace(self.query, adults=2, children=1, seat="special-first")
        self.adapter.query = self.query
        self.pages = [page([row(special="11")])]
        self.login()
        store = AttemptStore(self.root / "attempts.json")
        with patch.object(self.client.api, "reserve", wraps=self.client.api.reserve) as reserve:
            result = watch(self.adapter, self.query, self.budget, store, emit=Mock())
        self.assertEqual(result, "reserved")
        reserve.assert_called_once()
        consent = reserve.call_args.kwargs["consent"]
        self.assertTrue(consent.allow_reserve)
        self.assertFalse(consent.dry_run)
        self.assertFalse(consent.allow_payment or consent.allow_cancel or consent.allow_refund)
        form = self.calls[-1][1]
        self.assertTrue(self.calls[-1][0].endswith("TicketReservation"))
        self.assertEqual(form["txtTotPsgCnt"], ["3"])
        self.assertEqual(form["txtPsrmClCd1"], ["2"])
        self.assertNotIn("private-", store.path.read_text() + self.audit.path.read_text())

    def test_reserve_requires_explicit_mode_and_matching_train(self):
        self.login()
        train = self.adapter.search(self.query)[0]
        count = len(self.calls)
        for query in (replace(self.query, reserve=False), replace(self.query, train_number="999")):
            with self.assertRaises(StopRun):
                self.client.reserve(train, query)
        self.assertEqual(len(self.calls), count)

    def test_budget_counts_bootstrap_and_prevents_login_at_cap(self):
        self.budget.policy = Policy(max_requests=2)
        with self.assertRaises(StopRun):
            self.login()
        self.assertEqual(len(self.calls), 2)
        self.assertFalse(self.client.logined)

    def test_new_client_error_types_preserve_retry_policy(self):
        self.assertEqual(error_kind(KorailSessionExpiredError("P058", "expired")), "session")
        try:
            try:
                raise httpx.ReadTimeout("private-url")
            except httpx.ReadTimeout as exc:
                raise KorailTransportError("transport") from exc
        except KorailTransportError as exc:
            self.assertEqual(error_kind(exc), "transient")


if __name__ == "__main__":
    unittest.main()
