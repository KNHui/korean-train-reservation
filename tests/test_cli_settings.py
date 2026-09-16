import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from keyring.errors import PasswordDeleteError

from cli_settings import create_app
from train_cli import configure_stations


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.keyring = SimpleNamespace(get_password=Mock(return_value=None),
                                           set_password=Mock(), delete_password=Mock())
        self.app.inquirer = Mock()
        configure_stations(self.app)

    def test_custom_station_edit_normalizes_and_saves_ktx_favorites(self):
        self.app.inquirer.prompt.return_value = {"stations": " 김천(구미),김천구미,안동,수서 "}
        self.assertTrue(self.app.edit_station())
        self.app.keyring.set_password.assert_called_once_with("KTX", "station", "김천구미,안동,수서")

    def test_cancelled_settings_do_not_write_credentials(self):
        self.app.inquirer.prompt.return_value = None
        for action in (self.app.set_station, self.app.edit_station, self.app.set_telegram,
                       self.app.set_card):
            self.assertFalse(action())
        self.app.keyring.set_password.assert_not_called()

    def test_telegram_settings_use_existing_keys_without_sending(self):
        self.app.inquirer.prompt.return_value = {"token": " test-token ", "chat_id": " test-chat "}
        with patch("requests.post") as send, patch("builtins.print"):
            self.assertTrue(self.app.set_telegram())
        send.assert_not_called()
        self.app.keyring.set_password.assert_any_call("telegram", "token", "test-token")
        self.app.keyring.set_password.assert_any_call("telegram", "chat_id", "test-chat")

    def test_card_settings_are_validated_before_they_are_stored(self):
        self.app.inquirer.prompt.return_value = {
            "number": " 1234567812345678 ", "password": "12",
            "birthday": "900101", "expire": "2812",
        }
        with patch("builtins.print") as emit:
            self.assertTrue(self.app.set_card())
        self.app.keyring.set_password.assert_any_call("card", "number", "1234567812345678")
        self.app.keyring.set_password.assert_any_call("card", "expire", "2812")
        self.app.keyring.set_password.assert_any_call("card", "ok", "1")
        self.assertNotIn("1234567812345678", str(emit.call_args_list))

    def test_an_unusable_card_is_refused_without_storing_a_partial_entry(self):
        self.app.inquirer.prompt.return_value = {
            "number": "1234-5678", "password": "12", "birthday": "900101", "expire": "2812",
        }
        with patch("builtins.print"):
            self.assertFalse(self.app.set_card())
        self.app.keyring.set_password.assert_not_called()

    def test_clearing_the_card_removes_every_entry_including_the_flag(self):
        with patch("builtins.print"):
            self.assertTrue(self.app.clear_card())
        for name in ("number", "password", "birthday", "expire", "ok"):
            self.app.keyring.delete_password.assert_any_call("card", name)

    def test_clearing_tolerates_entries_the_keyring_no_longer_holds(self):
        self.app.keyring.delete_password.side_effect = PasswordDeleteError("missing")
        with patch("builtins.print"):
            self.assertTrue(self.app.clear_card())
        self.assertEqual(self.app.keyring.delete_password.call_count, 5)

    def test_a_refused_deletion_is_reported_instead_of_claiming_success(self):
        self.app.keyring.delete_password.side_effect = RuntimeError("denied")
        self.app.keyring.get_password.return_value = "still-here"
        with patch("builtins.print") as emit:
            self.assertFalse(self.app.clear_card())
        self.assertIn("삭제하지 못했습니다", str(emit.call_args_list))

    def test_a_backend_that_keeps_the_entry_is_not_reported_as_cleared(self):
        # Deletion raises nothing, but the card survives it.
        self.app.keyring.get_password.return_value = "still-here"
        with patch("builtins.print"):
            self.assertFalse(self.app.clear_card())

    def test_an_unreadable_keyring_is_not_reported_as_cleared(self):
        self.app.keyring.get_password.side_effect = RuntimeError("locked")
        with patch("builtins.print"):
            self.assertFalse(self.app.clear_card())

    def test_missing_ktx_preferences_do_not_read_other_accounts(self):
        from train_cli import configure_options
        configure_options(self.app)
        self.assertEqual(self.app.get_options(), ["ktx"])
        self.app.keyring.get_password.assert_called_once_with("KTX", "options")
