import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from train_cli import configure_options, main, run_menu


class MenuTests(unittest.TestCase):
    def make_app(self):
        return SimpleNamespace(
            inquirer=SimpleNamespace(list_input=Mock(), prompt=Mock(), Checkbox=Mock()),
            keyring=SimpleNamespace(get_password=Mock(return_value=None), set_password=Mock()),
            **{name: Mock() for name in (
                "reserve", "check_reservation", "set_login", "set_telegram",
                "set_station", "edit_station", "set_options",
            )},
        )

    def test_actions_use_korail_without_an_operator_prompt(self):
        app = self.make_app()
        app.inquirer.list_input.side_effect = [
            "reserve", "reservations", "login", "stations", "edit_stations",
            "telegram", "passengers", "exit",
        ]
        run_menu(app, debug=True)
        for name in ("reserve", "check_reservation", "set_login"):
            getattr(app, name).assert_called_once_with("KTX", True)
        for name in ("set_station", "edit_station"):
            getattr(app, name).assert_called_once_with("KTX")
        for name in ("set_telegram", "set_options"):
            getattr(app, name).assert_called_once_with()
        self.assertEqual(app.inquirer.list_input.call_count, 8)
        for call in app.inquirer.list_input.call_args_list:
            self.assertNotIn("열차 선택", call.kwargs["message"])
            self.assertNotIn("SRT", str(call.kwargs["choices"]))
            self.assertNotIn("card", dict((value, label) for label, value in call.kwargs["choices"]))

    def test_ktx_passenger_preferences_are_kept(self):
        app = self.make_app()
        app.keyring.get_password.return_value = "child,senior"
        configure_options(app)
        self.assertEqual(app.get_options(), ["child", "senior", "ktx"])

    def test_explicitly_empty_new_preferences_do_not_restore_legacy_settings(self):
        app = self.make_app()
        app.keyring.get_password.return_value = ""
        configure_options(app)
        self.assertEqual(app.get_options(), ["ktx"])
        app.keyring.get_password.assert_called_once_with("KTX", "options")

    def test_passenger_settings_remove_train_filter_and_save_under_korail(self):
        app = self.make_app()
        app.keyring.get_password.return_value = "child,ktx"
        app.inquirer.prompt.return_value = {"options": ["senior"]}
        configure_options(app)
        app.set_options()
        question = app.inquirer.Checkbox.call_args.kwargs
        self.assertNotIn(("KTX만", "ktx"), question["choices"])
        self.assertEqual(question["default"], ["child"])
        app.keyring.set_password.assert_called_once_with("KTX", "options", "senior")

    def test_cancel_does_not_change_preferences(self):
        app = self.make_app()
        app.inquirer.prompt.return_value = None
        configure_options(app)
        app.set_options()
        app.keyring.set_password.assert_not_called()

    def test_entrypoint_uses_project_menu(self):
        app = self.make_app()
        app.STATIONS = {}
        app.DEFAULT_STATIONS = {}
        app.get_station = Mock()
        app.inquirer.list_input.return_value = "exit"
        original_inquirer = app.inquirer
        with patch("train_cli.importlib.import_module", return_value=SimpleNamespace(create_app=lambda: app)):
            main([])
        original_inquirer.list_input.assert_called_once()
        from terminal_input import NavigationRender
        self.assertIsInstance(original_inquirer.list_input.call_args.kwargs["render"], NavigationRender)


if __name__ == "__main__":
    unittest.main()
