import contextlib
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from reservation_guard import LoginFailure
from train_cli import configure_login


class LoginSettingsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.client = Mock(logined=False)

    def login_success(self):
        self.client.logined = True
        return True

    def make_app(self):
        return SimpleNamespace(
            keyring=SimpleNamespace(get_password=Mock(return_value="saved-value"), set_password=Mock()),
            inquirer=SimpleNamespace(Text=Mock(), Password=Mock(), prompt=Mock(return_value={"id": "private-id", "pass": "private-password"})),
            Korail=Mock(return_value=self.client),
        )

    def test_settings_failure_preserves_saved_credentials_and_closes_session(self):
        app = self.make_app()
        output = io.StringIO()
        with patch("reservation_guard.RUNTIME", self.root), patch("reservation_guard.create_client", app.Korail):
            configure_login(app)
        with patch.object(self.client, "login", side_effect=LoginFailure("macro_blocked")), patch.object(self.client, "close") as close, contextlib.redirect_stdout(output):
            self.assertFalse(app.set_login("KTX", debug=True))
        app.keyring.set_password.assert_not_called()
        app.Korail.assert_called_once_with("KTX", "private-id", "private-password")
        close.assert_called_once()
        self.assertIn("MACRO ERROR", output.getvalue())
        self.assertNotIn("private-", output.getvalue())

    def test_settings_success_saves_only_after_authentication(self):
        app = self.make_app()
        app.keyring.set_password.side_effect = lambda *args: self.assertTrue(self.client.logined)
        with patch("reservation_guard.RUNTIME", self.root), patch("reservation_guard.create_client", app.Korail):
            configure_login(app)
        with patch.object(self.client, "login", side_effect=self.login_success), contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(app.set_login("KTX"))
        self.assertEqual(app.keyring.set_password.call_count, 3)
        app.keyring.set_password.assert_any_call("KTX", "ok", "1")

    def test_settings_cancel_does_not_connect_or_save(self):
        app = self.make_app()
        app.inquirer.prompt.return_value = None
        configure_login(app)
        self.assertFalse(app.set_login("KTX"))
        app.Korail.assert_not_called()
        app.keyring.set_password.assert_not_called()


if __name__ == "__main__":
    unittest.main()
