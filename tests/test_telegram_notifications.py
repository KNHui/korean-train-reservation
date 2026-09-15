import unittest
from unittest.mock import Mock, call, patch

from telegram_notifications import TelegramNotifier, load_notifier


class TelegramTests(unittest.TestCase):
    def test_uses_existing_menu_settings_without_exposing_credentials(self):
        emit = Mock()
        with patch("telegram_notifications.keyring.get_password", side_effect=[" test-token ", " 123 "]) as read:
            notifier = load_notifier(emit)
        self.assertIsInstance(notifier, TelegramNotifier)
        self.assertEqual(notifier.token, "test-token")
        self.assertEqual(notifier.chat_id, "123")
        self.assertEqual(read.call_args_list, [call("telegram", "token"), call("telegram", "chat_id")])
        self.assertNotIn("test-token", str(emit.call_args_list))

    def test_missing_settings_disable_notifications(self):
        for settings in ([None, None], ["token", None], [None, "123"], [" ", "123"]):
            with self.subTest(settings=settings), \
                    patch("telegram_notifications.keyring.get_password", side_effect=settings), \
                    patch("telegram_notifications.requests.post") as post:
                self.assertIsNone(load_notifier(Mock()))
                post.assert_not_called()

    def test_keyring_error_is_reported_without_secret(self):
        emit = Mock()
        with patch("telegram_notifications.keyring.get_password", side_effect=RuntimeError("secret")):
            self.assertIsNone(load_notifier(emit))
        self.assertNotIn("secret", str(emit.call_args_list))

    def test_success_message_contains_reservation_and_payment_reminder(self):
        response = Mock(status_code=200)
        response.json.return_value = {"ok": True}
        with patch("telegram_notifications.requests.post") as post:
            post.return_value.__enter__.return_value = response
            TelegramNotifier("test-token", "123")("2026-09-24 수서 → 동대구 12:30 일반실")
        post.assert_called_once()
        self.assertEqual(post.call_args.args, ("https://api.telegram.org/bottest-token/sendMessage",))
        kwargs = post.call_args.kwargs
        self.assertEqual(kwargs["json"]["chat_id"], "123")
        self.assertIn("예약 성공", kwargs["json"]["text"])
        self.assertIn("수서 → 동대구 12:30 일반실", kwargs["json"]["text"])
        self.assertIn("구입기한", kwargs["json"]["text"])
        self.assertNotIn("parse_mode", kwargs["json"])
        self.assertEqual(kwargs["timeout"], 10)
        self.assertFalse(kwargs["allow_redirects"])

    def test_rejection_and_invalid_response_are_failures_without_retry(self):
        for status, payload in ((403, {}), (302, {}), (429, {}), (200, {"ok": False}), (200, {})):
            with self.subTest(status=status, payload=payload), \
                    patch("telegram_notifications.requests.post") as post:
                response = post.return_value.__enter__.return_value
                response.status_code = status
                response.json.return_value = payload
                with self.assertRaises(RuntimeError):
                    TelegramNotifier("test-token", "123")("reservation")
                post.assert_called_once()

    def test_long_reservation_keeps_message_below_limit_with_payment_reminder(self):
        with patch("telegram_notifications.requests.post") as post:
            response = post.return_value.__enter__.return_value
            response.status_code = 200
            response.json.return_value = {"ok": True}
            TelegramNotifier("test-token", "123")("가" * 5000)
        message = post.call_args.kwargs["json"]["text"]
        self.assertLessEqual(len(message), 4096)
        self.assertTrue(message.endswith("결제는 공식 앱에서 구입기한 내에 완료하세요."))


if __name__ == "__main__":
    unittest.main()
