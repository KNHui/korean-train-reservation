import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from cli_settings import create_app
from train_cli import configure_stations


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.keyring = SimpleNamespace(get_password=Mock(return_value=None), set_password=Mock())
        self.app.inquirer = Mock()
        configure_stations(self.app)

    def test_custom_station_edit_normalizes_and_saves_ktx_favorites(self):
        self.app.inquirer.prompt.return_value = {"stations": " 김천(구미),김천구미,안동,수서 "}
        self.assertTrue(self.app.edit_station())
        self.app.keyring.set_password.assert_called_once_with("KTX", "station", "김천구미,안동,수서")

    def test_cancelled_settings_do_not_write_credentials(self):
        self.app.inquirer.prompt.return_value = None
        for action in (self.app.set_station, self.app.edit_station, self.app.set_telegram):
            self.assertFalse(action())
        self.app.keyring.set_password.assert_not_called()

    def test_telegram_settings_use_existing_keys_without_sending(self):
        self.app.inquirer.prompt.return_value = {"token": " test-token ", "chat_id": " test-chat "}
        with patch("requests.post") as send, patch("builtins.print"):
            self.assertTrue(self.app.set_telegram())
        send.assert_not_called()
        self.app.keyring.set_password.assert_any_call("telegram", "token", "test-token")
        self.app.keyring.set_password.assert_any_call("telegram", "chat_id", "test-chat")

    def test_missing_ktx_preferences_do_not_read_other_accounts(self):
        from train_cli import configure_options
        configure_options(self.app)
        self.assertEqual(self.app.get_options(), ["ktx"])
        self.app.keyring.get_password.assert_called_once_with("KTX", "options")
