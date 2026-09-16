import contextlib
from collections import deque
from dataclasses import replace
import io
from pathlib import Path
import random
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from reservation_guard import (
    Adapter, AttemptStore, Audit, Budget, IntervalQueue, Policy, Query, StopRun,
    main, retry_read_operation, single_instance, watch,
)
from train_cli import configure_reservation


class FakeClock:
    def __init__(self):
        self.now = 0
        self.waits = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


def train(number="001", seat=True):
    return SimpleNamespace(dep_date="20990101", train_no=number, dep_name="서울",
                           arr_name="부산", dep_time="120000", seat=seat)


class IntervalQueueTests(unittest.TestCase):
    def test_fifo_keeps_ten_bounded_variable_waits(self):
        queue = IntervalQueue(Policy(), rng=random.Random(42))
        expected = list(queue.pending)
        actual = [queue.next_delay() for _ in range(10)]
        self.assertEqual(actual, expected)
        for _ in range(1000):
            actual.append(queue.next_delay())
            self.assertEqual(len(queue.pending), 10)
            self.assertTrue(all(15 <= delay <= 30 for delay in queue.pending))
        self.assertTrue(all(15 <= delay <= 30 for delay in actual))
        self.assertGreater(len(set(actual)), 10)

    def test_refill_compensates_for_remaining_queue(self):
        for queued, expected_range in ((20, (22.5, 30)), (25, (15, 22.5)),
                                        (22.5, (15, 30))):
            with self.subTest(queued=queued):
                rng = Mock()
                rng.uniform.side_effect = lambda low, high: (low + high) / 2
                queue = IntervalQueue(Policy(), rng=rng)
                queue.pending = deque([queued] * 10)
                rng.reset_mock()
                self.assertEqual(queue.next_delay(), queued)
                rng.uniform.assert_called_once_with(*expected_range)
                self.assertEqual(list(queue.pending)[:9], [queued] * 9)
                self.assertEqual(len(queue.pending), 10)

    def test_custom_range_and_fixed_interval(self):
        for minimum, maximum in ((15, 180), (120, 240), (60, 60)):
            with self.subTest(minimum=minimum, maximum=maximum):
                queue = IntervalQueue(Policy(interval=minimum, max_interval=maximum),
                                      rng=random.Random(1))
                waits = [queue.next_delay() for _ in range(100)]
                self.assertTrue(all(minimum <= delay <= maximum for delay in waits))


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.clock = FakeClock()
        self.audit = Audit(self.root / "events.jsonl")
        self.store = AttemptStore(self.root / "attempts.json")
        self.query = Query("KTX", "서울", "부산", "20990101", "110000", "130000")
        self.adapter = Mock()
        self.adapter.search.return_value = []
        self.adapter.available.side_effect = lambda t: t.seat
        self.adapter.reservations.return_value = []
        self.emit = Mock()
        self.notifier = Mock()

    def budget(self, **kwargs):
        return Budget(Policy(**kwargs), self.audit, self.clock, self.clock.sleep)

    def booking_query(self):
        return Query("KTX", "서울", "부산", "20990101", "110000", "130000", "1", True)

    def paying_query(self):
        return replace(self.booking_query(), pay=True)

    def test_payment_requires_the_reservation_mode(self):
        with self.assertRaises(ValueError):
            replace(self.query, pay=True).validate()
        self.paying_query().validate()

    def test_a_card_never_reaches_a_notify_only_run(self):
        self.adapter.search.return_value = [train()]
        card = object()
        result = watch(self.adapter, self.query, self.budget(), self.store,
                       self.emit, self.notifier, card=card)
        self.assertEqual(result, "available")
        self.adapter.pay.assert_not_called()

    def test_a_permanently_unreadable_ticket_list_stops_instead_of_asking_forever(self):
        selected = train()
        self.adapter.search.return_value = [selected]
        self.adapter.reserve.return_value = selected
        self.adapter.payable.return_value = True
        self.adapter.pay.side_effect = ValueError("rejected")
        self.adapter.paid_tickets.side_effect = TimeoutError()
        result = watch(self.adapter, self.paying_query(), self.budget(), self.store,
                       self.emit, self.notifier, card=object())
        self.assertEqual(result, "payment_uncertain")
        self.adapter.pay.assert_called_once()
        self.assertEqual(self.adapter.paid_tickets.call_count, Policy().max_errors)
        self.assertEqual(self.store.state(selected), "payment_uncertain")

    def test_a_payment_is_recorded_before_it_is_sent_so_a_crash_is_not_replayed(self):
        selected = train()
        self.adapter.search.return_value = [selected]
        self.adapter.reserve.return_value = selected
        self.adapter.payable.return_value = True
        self.adapter.pay.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            watch(self.adapter, self.paying_query(), self.budget(), self.store,
                  self.emit, self.notifier, card=object())
        self.assertEqual(self.store.state(selected), "payment_uncertain")
        self.assertTrue(AttemptStore(self.root / "attempts.json").contains(selected))

    def test_a_paid_run_notifies_as_paid(self):
        selected = train()
        self.adapter.search.return_value = [selected]
        self.adapter.reserve.return_value = selected
        self.adapter.payable.return_value = True
        self.adapter.pay.return_value = True
        result = watch(self.adapter, self.paying_query(), self.budget(), self.store,
                       self.emit, self.notifier, card=object())
        self.assertEqual(result, "paid")
        self.notifier.assert_called_once_with(selected, True)
        self.assertEqual(self.store.state(selected), "paid")

    def test_notify_never_reserves_or_reads_reservations(self):
        self.adapter.search.return_value = [train()]
        result = watch(self.adapter, self.query, self.budget(), self.store, self.emit, self.notifier)
        self.assertEqual(result, "available")
        self.adapter.reserve.assert_not_called()
        self.adapter.reservations.assert_not_called()
        self.notifier.assert_not_called()

    def test_preparation_retries_then_reports_exhausted_connection_error(self):
        operation = Mock(side_effect=TimeoutError("private-token"))
        with self.assertRaisesRegex(StopRun, "로그인.*3회.*TimeoutError"):
            retry_read_operation(operation, self.budget(), "로그인", self.emit)
        self.assertEqual(operation.call_count, 3)
        self.assertEqual(self.clock.waits, [120, 240])
        self.assertNotIn("private-token", str(self.emit.call_args_list)
                         + self.audit.path.read_text(encoding="utf-8"))

    def test_preparation_does_not_retry_restrictions_or_unknown_errors(self):
        for error in (StopRun("restricted"), RuntimeError("MACRO ERROR"), ValueError("protocol")):
            with self.subTest(error=type(error).__name__):
                operation = Mock(side_effect=error)
                with self.assertRaises(type(error)):
                    retry_read_operation(operation, self.budget(), "로그인", self.emit)
                operation.assert_called_once()
        self.assertEqual(self.clock.waits, [])

    def test_preparation_retry_obeys_deadline(self):
        operation = Mock(side_effect=TimeoutError())
        with self.assertRaises(StopRun):
            retry_read_operation(operation, self.budget(max_seconds=30), "로그인", self.emit)
        operation.assert_called_once()
        self.assertEqual(self.clock.waits, [30])

    def test_duplicate_check_retries_before_search_and_still_prevents_booking(self):
        selected = train()
        self.adapter.reservations.side_effect = [TimeoutError(), [selected]]
        self.adapter.search.return_value = [selected]
        self.assertEqual(watch(self.adapter, self.booking_query(), self.budget(),
                               self.store, self.emit), "existing")
        self.assertEqual(self.clock.waits, [120])
        self.adapter.reserve.assert_not_called()

    def test_main_recovers_from_initial_login_timeout(self):
        with patch("reservation_guard.RUNTIME", self.root), \
                patch("reservation_guard.credentials", return_value=("user", "password")), \
                patch("reservation_guard.create_client", return_value=Mock()), \
                patch("reservation_guard.Adapter") as adapter, \
                patch("reservation_guard.Budget.wait") as wait, \
                patch("reservation_guard.watch", return_value="limit") as runner, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            adapter.return_value.login.side_effect = [TimeoutError(), None]
            result = main(["--dep", "서울", "--arr", "부산", "--date", "20990101"])
        self.assertEqual(result, 0)
        self.assertEqual(adapter.return_value.login.call_count, 2)
        wait.assert_called_once_with(120)
        runner.assert_called_once()
        self.assertIn("로그인: 통신 오류", output.getvalue())

    def test_sold_out_stops_at_search_limit_and_reuses_client(self):
        watch(self.adapter, self.query, self.budget(max_searches=3), self.store, self.emit, self.notifier)
        self.assertEqual(self.adapter.search.call_count, 3)
        self.assertEqual(len(self.clock.waits), 2)
        self.assertTrue(all(15 <= delay <= 30 for delay in self.clock.waits))
        self.adapter.login.assert_not_called()
        self.notifier.assert_not_called()

    def test_deadline_does_not_allow_next_search(self):
        with self.assertRaises(StopRun):
            watch(self.adapter, self.query,
                  self.budget(interval=60, max_interval=60, max_seconds=90), self.store, self.emit)
        self.assertEqual(self.adapter.search.call_count, 2)
        self.assertEqual(self.clock(), 90)

    def test_watch_uses_refilled_queue_after_ten_waits(self):
        queue = IntervalQueue(Policy(), rng=random.Random(42))
        expected = [queue.next_delay() for _ in range(24)]
        with patch("reservation_guard.IntervalQueue", return_value=IntervalQueue(
                Policy(), rng=random.Random(42))):
            result = watch(self.adapter, self.query,
                           self.budget(max_searches=25, max_seconds=3600), self.store, self.emit)
        self.assertEqual(result, "limit")
        self.assertEqual(self.adapter.search.call_count, 25)
        self.assertEqual(self.clock.waits, expected)

    def test_default_run_reserves_after_exceeding_all_previous_limits(self):
        selected = train()
        session = SimpleNamespace(request=Mock(return_value=Mock(status_code=200)))
        budget = self.budget()
        budget.wrap(session)

        def search(query):
            session.request("GET", "https://example.invalid/search")
            return [selected] if budget.requests == 101 else []

        self.adapter.search.side_effect = search
        self.adapter.reserve.return_value = selected
        result = watch(self.adapter, self.booking_query(), budget, self.store, self.emit, self.notifier)
        self.assertEqual(result, "reserved")
        self.assertEqual(self.adapter.search.call_count, 101)
        self.assertEqual(budget.requests, 101)
        self.assertGreater(self.clock(), 900)
        self.assertEqual(len(self.clock.waits), 100)
        self.assertTrue(all(15 <= delay <= 30 for delay in self.clock.waits))
        self.adapter.reserve.assert_called_once_with(selected)
        self.notifier.assert_called_once_with(selected, False)

    def test_keyboard_interrupt_stops_unlimited_wait_before_next_search(self):
        def interrupt(seconds):
            raise KeyboardInterrupt

        budget = Budget(Policy(), self.audit, self.clock, interrupt)
        with self.assertRaises(KeyboardInterrupt):
            watch(self.adapter, self.query, budget, self.store, self.emit)
        self.adapter.search.assert_called_once()
        self.adapter.reserve.assert_not_called()

    def test_main_closes_client_on_manual_stop(self):
        client = Mock()
        with patch("reservation_guard.RUNTIME", self.root), \
                patch("reservation_guard.credentials", return_value=("user", "password")), \
                patch("reservation_guard.create_client", return_value=client), \
                patch("reservation_guard.Adapter"), \
                patch("reservation_guard.watch", side_effect=KeyboardInterrupt), \
                patch("reservation_guard.close_client") as close, \
                contextlib.redirect_stdout(io.StringIO()):
            result = main(["--dep", "서울", "--arr", "부산", "--date", "20990101"])
        self.assertEqual(result, 130)
        close.assert_called_once_with(client)

    def test_transient_errors_back_off_and_stop(self):
        self.adapter.search.side_effect = TimeoutError("secret-token")
        with self.assertRaises(StopRun):
            watch(self.adapter, self.query, self.budget(), self.store, self.emit)
        self.assertEqual(self.clock.waits, [120, 240])
        self.assertEqual(self.adapter.search.call_count, 3)
        self.assertNotIn("secret-token", (self.root / "events.jsonl").read_text())
        self.adapter.login.assert_not_called()

    def test_macro_warning_stops_without_more_requests(self):
        self.adapter.search.side_effect = RuntimeError("MACRO ERROR private token")
        with self.assertRaises(StopRun):
            watch(self.adapter, self.query, self.budget(), self.store, self.emit)
        self.assertEqual(self.adapter.search.call_count, 1)
        self.adapter.login.assert_not_called()

    def test_expired_session_can_only_reauthenticate_once(self):
        error = type("KorailSessionExpiredError", (Exception,), {})
        self.adapter.search.side_effect = error()
        with self.assertRaises(StopRun):
            watch(self.adapter, self.query, self.budget(), self.store, self.emit)
        self.adapter.login.assert_called_once()
        self.assertEqual(self.adapter.search.call_count, 2)

    def test_reservation_uses_number_not_result_position(self):
        selected = train("00001")
        self.adapter.search.side_effect = [[train("2")], [train("3"), selected]]
        self.adapter.reserve.return_value = selected
        result = watch(self.adapter, self.booking_query(), self.budget(), self.store, self.emit, self.notifier)
        self.assertEqual(result, "reserved")
        self.adapter.reserve.assert_called_once_with(selected)
        self.adapter.pay_with_card.assert_not_called()
        self.notifier.assert_called_once_with(selected, False)

    def test_discovery_calls_reserve_before_output_audit_or_state_write(self):
        selected = train()
        self.adapter.search.return_value = [selected]
        audit = Mock()
        budget = self.budget()
        budget.audit = audit
        with patch.object(self.store, "record", wraps=self.store.record) as record:
            def reserve(found):
                self.assertIs(found, selected)
                self.adapter.reservations.assert_called_once()
                self.emit.assert_not_called()
                audit.assert_not_called()
                record.assert_not_called()
                self.assertFalse(self.store.path.exists())
                return selected

            self.adapter.reserve.side_effect = reserve
            self.assertEqual(watch(self.adapter, self.booking_query(), budget,
                                   self.store, self.emit), "reserved")
            record.assert_called_once_with(selected, "reserved")

    def test_logging_failure_does_not_prevent_reservation_attempt(self):
        selected = train()
        self.adapter.search.return_value = [selected]
        self.adapter.reserve.return_value = selected
        budget = self.budget()
        budget.audit = Mock(side_effect=OSError("audit unavailable"))
        with self.assertRaises(OSError):
            watch(self.adapter, self.booking_query(), budget, self.store, self.emit)
        self.adapter.reserve.assert_called_once_with(selected)
        self.assertEqual(self.store.states[self.store.key(selected)]["state"], "reserved")

    def test_interruption_during_reserve_records_uncertain_after_call(self):
        selected = train()
        self.adapter.search.return_value = [selected]
        self.adapter.reserve.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            watch(self.adapter, self.booking_query(), self.budget(), self.store, self.emit)
        self.assertEqual(AttemptStore(self.store.path).states[
            self.store.key(selected)]["state"], "uncertain")

    def test_notification_failure_preserves_success_and_prevents_rereservation(self):
        selected = train()
        self.adapter.search.return_value = [selected]
        self.adapter.reserve.return_value = selected

        def fail_notification(reservation):
            # Success must be durable before attempting notification delivery.
            state = AttemptStore(self.store.path).states[self.store.key(selected)]
            self.assertEqual(state["state"], "reserved")
            raise TimeoutError("secret-bot-token private-chat-id")

        self.notifier.side_effect = fail_notification
        result = watch(self.adapter, self.booking_query(), self.budget(), self.store, self.emit, self.notifier)
        self.assertEqual(result, "reserved")
        with self.assertRaises(StopRun):
            watch(self.adapter, self.booking_query(), self.budget(),
                  AttemptStore(self.store.path), self.emit, self.notifier)
        self.adapter.reserve.assert_called_once()
        self.notifier.assert_called_once_with(selected, False)
        self.assertEqual(self.adapter.reservations.call_count, 2)
        output = str(self.emit.call_args_list) + self.audit.path.read_text(encoding="utf-8")
        self.assertIn("텔레그램 알림 전송에 실패", output)
        self.assertNotIn("secret-bot-token", output)
        self.assertNotIn("private-chat-id", output)

    def test_time_window_reserves_first_available_match_without_train_number(self):
        query = replace(self.query, reserve=True)
        query.validate()
        selected = train("003")
        outside = [
            SimpleNamespace(**{**vars(train()), **changes}) for changes in (
                {"dep_time": "105959"}, {"dep_time": "130001"},
                {"dep_date": "20990102"}, {"dep_name": "대전"}, {"arr_name": "대전"},
            )
        ]
        self.adapter.search.side_effect = [
            outside + [train("002", seat=False)],
            outside + [train("002", seat=False), selected, train("004")],
        ]
        self.adapter.reserve.return_value = selected
        result = watch(self.adapter, query, self.budget(), self.store, self.emit)
        self.assertEqual(result, "reserved")
        self.adapter.reserve.assert_called_once_with(selected)
        self.assertEqual(len(self.clock.waits), 1)
        self.assertTrue(15 <= self.clock.waits[0] <= 30)
        self.adapter.pay_with_card.assert_not_called()

    def test_existing_paid_or_unpaid_ticket_prevents_duplicate(self):
        selected = train()
        self.adapter.search.return_value = [selected]
        self.adapter.reservations.return_value = [selected]
        result = watch(self.adapter, self.booking_query(), self.budget(), self.store, self.emit, self.notifier)
        self.assertEqual(result, "existing")
        self.adapter.reserve.assert_not_called()
        self.notifier.assert_not_called()

    def test_timeout_after_success_reconciles_without_second_reservation(self):
        selected = train()
        self.adapter.search.return_value = [selected]
        self.adapter.reservations.side_effect = [[], [selected]]
        self.adapter.reserve.side_effect = TimeoutError()
        result = watch(self.adapter, self.booking_query(), self.budget(), self.store, self.emit, self.notifier)
        self.assertEqual(result, "reserved")
        self.assertEqual(self.adapter.reserve.call_count, 1)
        self.assertEqual(self.adapter.reservations.call_count, 2)
        self.notifier.assert_called_once_with(selected, False)

    def test_failed_reservation_retries_after_two_empty_history_checks(self):
        selected = train()
        self.adapter.search.return_value = [selected]
        self.adapter.reserve.side_effect = [TimeoutError(), selected]
        result = watch(self.adapter, self.booking_query(), self.budget(), self.store, self.emit, self.notifier)
        self.assertEqual(result, "reserved")
        self.assertEqual(self.adapter.reserve.call_count, 2)
        self.assertEqual(self.adapter.reservations.call_count, 3)
        self.assertEqual(len(self.clock.waits), 2)
        self.notifier.assert_called_once_with(selected, False)

    def test_delayed_history_success_does_not_reserve_twice(self):
        selected = train()
        self.adapter.search.return_value = [selected]
        self.adapter.reserve.side_effect = TimeoutError()
        self.adapter.reservations.side_effect = [[], [], [selected]]
        self.assertEqual(watch(self.adapter, self.booking_query(), self.budget(),
                               self.store, self.emit, self.notifier), "reserved")
        self.adapter.reserve.assert_called_once()
        self.assertEqual(len(self.clock.waits), 1)

    def test_history_errors_keep_checking_without_reserving_again(self):
        selected = train()
        self.adapter.search.return_value = [selected]
        self.adapter.reserve.side_effect = TimeoutError()
        self.adapter.reservations.side_effect = [[], [], TimeoutError(), [], [selected]]
        self.assertEqual(watch(self.adapter, self.booking_query(), self.budget(),
                               self.store, self.emit, self.notifier), "reserved")
        self.adapter.reserve.assert_called_once()
        self.assertEqual(len(self.clock.waits), 3)

    def test_uncertain_record_reconciles_after_restart(self):
        selected = train()
        self.store.record(selected, "uncertain")
        self.adapter.search.return_value = [selected]
        self.adapter.reserve.return_value = selected
        self.assertEqual(watch(self.adapter, self.booking_query(), self.budget(),
                               AttemptStore(self.store.path), self.emit), "reserved")
        self.adapter.reserve.assert_called_once()
        self.assertEqual(self.adapter.reservations.call_count, 3)
        self.assertEqual(self.adapter.search.call_count, 2)

    def test_protocol_errors_continue_until_explicit_search_limit(self):
        self.adapter.search.return_value = [train()]
        self.adapter.reserve.side_effect = type("KorailProtocolError", (Exception,), {})()
        self.assertEqual(watch(self.adapter, self.booking_query(), self.budget(max_searches=4),
                               self.store, self.emit), "limit")
        self.assertEqual(self.adapter.reserve.call_count, 4)

    def test_reconciliation_restriction_stops_and_retains_uncertain_record(self):
        selected = train()
        self.adapter.search.return_value = [selected]
        self.adapter.reserve.side_effect = TimeoutError()
        self.adapter.reservations.side_effect = [[], StopRun("restricted")]
        with self.assertRaises(StopRun):
            watch(self.adapter, self.booking_query(), self.budget(), self.store, self.emit)
        self.adapter.reserve.assert_called_once()
        self.assertEqual(self.store.state(selected), "uncertain")

    def test_main_connects_saved_notifier_only_for_auto_reservation(self):
        for reserve in (False, True):
            with self.subTest(reserve=reserve), \
                    patch("reservation_guard.RUNTIME", self.root), \
                    patch("reservation_guard.credentials", return_value=("user", "password")), \
                    patch("reservation_guard.create_client", return_value=Mock()), \
                    patch("reservation_guard.Adapter"), \
                    patch("reservation_guard.watch", return_value="limit") as runner, \
                    patch("telegram_notifications.load_notifier", return_value=self.notifier) as loader, \
                    contextlib.redirect_stdout(io.StringIO()):
                result = main(["--dep", "서울", "--arr", "부산", "--date", "20990101"]
                              + (["--reserve"] if reserve else []))
                self.assertEqual(result, 0)
                self.assertEqual(runner.call_args.args[2].policy, Policy())
                self.assertIs(runner.call_args.kwargs["notifier"], self.notifier if reserve else None)
                self.assertEqual(loader.call_count, int(reserve))

    def test_explicit_restriction_during_reservation_does_not_reconcile(self):
        self.adapter.search.return_value = [train()]
        self.adapter.reserve.side_effect = RuntimeError("MACRO ERROR")
        with self.assertRaises(StopRun):
            watch(self.adapter, self.booking_query(), self.budget(), self.store, self.emit)
        self.adapter.reservations.assert_called_once()

    def test_transport_budget_covers_both_sessions_and_stops_at_cap(self):
        response = Mock(status_code=200)
        first, second = Mock(), Mock()
        first.request.return_value = second.request.return_value = response
        underlying_first, underlying_second = first.request, second.request
        client = SimpleNamespace(_session=first, _netfunnel=SimpleNamespace(_session=second))
        budget = self.budget(max_requests=2)
        budget.attach(client)
        first.request("GET", "https://example.invalid/private")
        second.request("POST", "https://example.invalid/private")
        with self.assertRaises(StopRun):
            second.request("GET", "https://example.invalid/private")
        underlying_first.assert_called_once()
        underlying_second.assert_called_once()
        self.assertEqual(underlying_first.call_args.kwargs["timeout"], 15)
        self.assertFalse(underlying_first.call_args.kwargs["allow_redirects"])
        self.assertNotIn("private", (self.root / "events.jsonl").read_text())

    def test_http_restrictions_are_not_transient(self):
        for status in (401, 403, 429, 302):
            session = SimpleNamespace(request=Mock(return_value=Mock(status_code=status)))
            underlying = session.request
            self.budget().wrap(session)
            with self.assertRaises(StopRun):
                session.request("GET", "unused")
            with self.assertRaises(StopRun):
                session.request("GET", "unused")
            underlying.assert_called_once()

    def test_lock_blocks_second_instance_and_releases(self):
        path = self.root / "guard.lock"
        with single_instance(path):
            with self.assertRaises(StopRun):
                with single_instance(path):
                    self.fail("Second instance acquired the lock")
        with single_instance(path):
            pass

    def test_invalid_input_never_loads_credentials(self):
        with patch("reservation_guard.credentials") as credentials, contextlib.redirect_stdout(io.StringIO()):
            result = main(["--dep", "서울", "--arr", "부산", "--date", "20200101"])
        self.assertEqual(result, 2)
        credentials.assert_not_called()

    def test_invalid_limits_and_train_number_rejected(self):
        for policy in (Policy(interval=3), Policy(interval=float("nan")),
                       Policy(interval=14.99), Policy(max_interval=14),
                       Policy(max_interval=float("nan")), Policy(max_interval=float("inf")),
                       Policy(max_seconds=float("inf")), Policy(max_searches=0)):
            with self.assertRaises(ValueError):
                policy.validate()
        with self.assertRaises(ValueError):
            replace(self.booking_query(), train_number="abc").validate()

    def test_cli_custom_interval_range_and_optional_limits_reach_runner(self):
        with patch("reservation_guard.RUNTIME", self.root), \
                patch("reservation_guard.credentials", return_value=("user", "password")), \
                patch("reservation_guard.create_client", return_value=Mock()), \
                patch("reservation_guard.Adapter"), \
                patch("reservation_guard.watch", return_value="limit") as runner, \
                contextlib.redirect_stdout(io.StringIO()):
            result = main(["--dep", "서울", "--arr", "부산", "--date", "20990101",
                           "--interval", "20", "--max-interval", "180",
                           "--max-searches", "25", "--max-minutes", "60", "--max-requests", "200"])
        self.assertEqual(result, 0)
        self.assertEqual(runner.call_args.args[2].policy,
                         Policy(interval=20, max_interval=180, max_searches=25,
                                max_seconds=3600, max_requests=200))

    def test_menu_allows_auto_reservation_with_empty_train_number(self):
        app = SimpleNamespace(get_station=Mock(return_value=(["서울", "부산"], ["서울", "부산"])),
                              get_options=Mock(return_value=[]), inquirer=Mock())
        app.inquirer.prompt.return_value = {
            "dep": "서울", "arr": "부산", "date": "20990101",
            "time": "110000", "until": "130000", "train-number": "", "reserve": True,
            "interval": "20", "max-interval": "40",
        }
        with patch("reservation_guard.main") as guarded, contextlib.redirect_stdout(io.StringIO()):
            configure_reservation(app)
            app.reserve()
        arguments = guarded.call_args.args[0]
        self.assertIn("--reserve", arguments)
        self.assertEqual(arguments[arguments.index("--train-number") + 1], "")
        self.assertEqual(arguments[arguments.index("--time") + 1], "110000")
        self.assertEqual(arguments[arguments.index("--until") + 1], "130000")
        self.assertEqual(arguments[arguments.index("--interval") + 1], "20")
        self.assertEqual(arguments[arguments.index("--max-interval") + 1], "40")

    def test_menu_uses_ktx_guard(self):
        app = SimpleNamespace(get_station=Mock(return_value=(["서울", "부산"], ["서울", "부산"])),
                              get_options=Mock(return_value=["child"]), inquirer=Mock())
        app.inquirer.prompt.return_value = {"dep": "서울", "arr": "부산", "date": "20990101",
                                           "train-number": "123", "reserve": True, "children": "1"}
        with patch("reservation_guard.main") as guarded, contextlib.redirect_stdout(io.StringIO()):
            configure_reservation(app, True, "123")
            app.reserve("KTX", debug=True)
        arguments = guarded.call_args_list[0].args[0]
        self.assertNotIn("--rail", arguments)
        self.assertIn("--reserve", arguments)
        self.assertEqual(arguments[arguments.index("--train-number") + 1], "123")
        self.assertEqual(arguments[arguments.index("--children") + 1], "1")
        guarded.assert_called_once()

    def test_menu_cancel_never_runs_client(self):
        app = SimpleNamespace(get_station=Mock(return_value=(["서울", "부산"], ["서울", "부산"])),
                              get_options=Mock(return_value=[]), inquirer=Mock())
        app.inquirer.prompt.return_value = None
        with patch("reservation_guard.main") as guarded:
            configure_reservation(app)
            app.reserve()
        guarded.assert_not_called()

    def test_removed_operator_option_is_rejected_before_login(self):
        with patch("reservation_guard.credentials") as credentials, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                main(["--rail", "SRT"])
        self.assertEqual(caught.exception.code, 2)
        credentials.assert_not_called()



if __name__ == "__main__":
    unittest.main()
