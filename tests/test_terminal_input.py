import contextlib
import io
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import inquirer
from inquirer.events import KeyEventGenerator
from readchar import key

from terminal_input import NavigationInquirer, NavigationRender, prompt_steps
from train_cli import configure_reservation


class NavigationTests(unittest.TestCase):
    def run_steps(self, questions, keys):
        events = KeyEventGenerator(key_generator=iter(keys).__next__)
        render = NavigationRender(event_generator=events, allow_back=True)
        with contextlib.redirect_stdout(io.StringIO()):
            return prompt_steps(questions, render)

    def test_home_end_edit_text_and_password_without_losing_arrow_delete_behavior(self):
        for question_type in (inquirer.Text, inquirer.Password):
            with self.subTest(question_type=question_type):
                result = self.run_steps([question_type("value", default="1234")], [
                    key.HOME, key.DELETE, "9", key.END, key.LEFT, key.BACKSPACE,
                    key.RIGHT, "5", key.ENTER,
                ])
                self.assertEqual(result, {"value": "9245"})

    def test_lists_wrap_and_home_end_jump_in_long_and_single_item_lists(self):
        for count in (1, 20):
            choices = [(f"역 {i}", str(i)) for i in range(count)]
            for keys, expected in (
                ([key.UP], str(count - 1)),
                ([key.END, key.DOWN], "0"),
                ([key.END, key.HOME], "0"),
                ([key.END], str(count - 1)),
            ):
                with self.subTest(count=count, keys=keys):
                    result = self.run_steps([inquirer.List("station", choices=choices)], keys + [key.ENTER])
                    self.assertEqual(result["station"], expected)

    def test_checkboxes_wrap_without_changing_selections_on_navigation(self):
        result = self.run_steps([inquirer.Checkbox("stations", choices=["a", "b", "c"])], [
            key.UP, key.SPACE, key.DOWN, key.SPACE, key.END, key.HOME, key.ENTER,
        ])
        self.assertEqual(result["stations"], ["c", "a"])

    def test_back_preserves_partial_edits_and_changes_previous_station(self):
        result = self.run_steps([
            inquirer.List("dep", choices=[("서울", "seoul"), ("부산", "busan")]),
            inquirer.Text("date", default="20261001"),
            inquirer.Confirm("reserve", default=False),
        ], [
            key.ENTER, key.END, key.BACKSPACE, "2", key.ESC,
            key.DOWN, key.ENTER, key.ENTER, "n",
        ])
        self.assertEqual(result, {"dep": "busan", "date": "20261002", "reserve": False})

    def test_back_preserves_empty_text_false_confirmation_and_literal_braces(self):
        result = self.run_steps([
            inquirer.Text("number", default="1"),
            inquirer.Confirm("reserve", default=True),
            inquirer.Text("note"),
        ], [
            key.BACKSPACE, key.ENTER, "n", "{", "x", "}", key.ESC,
            key.ESC, key.ENTER, key.ENTER, key.ENTER,
        ])
        self.assertEqual(result, {"number": "", "reserve": False, "note": "{x}"})

    def test_validation_still_runs_and_escape_can_leave_an_invalid_value(self):
        validator = Mock(side_effect=lambda answers, value: value.isdigit())
        result = self.run_steps([
            inquirer.Text("first", default="ok"),
            inquirer.Text("number", validate=validator),
        ], [key.ENTER, "x", key.ENTER, key.ESC, key.ENTER,
            key.BACKSPACE, "2", key.ENTER])
        self.assertEqual(result["number"], "2")
        self.assertEqual(validator.call_count, 2)

    def test_first_step_escape_and_control_c_cancel(self):
        for keys in ([key.ESC], [key.ENTER, key.CTRL_C]):
            result = self.run_steps([inquirer.Text("first"), inquirer.Text("second")], keys)
            self.assertIsNone(result)

    def test_reservation_revised_values_reach_runner_only_after_final_step(self):
        events = KeyEventGenerator(key_generator=iter([
            key.ENTER, key.ENTER, key.ESC, key.UP, key.ENTER,
            key.ENTER, key.ENTER, key.ENTER, key.ENTER, key.ENTER,
            key.ENTER, key.ENTER, key.ENTER, key.BACKSPACE, "2", key.ENTER, "y", "n",
        ]).__next__)
        app = SimpleNamespace(
            inquirer=NavigationInquirer(inquirer),
            get_station=Mock(return_value=([], ["서울", "대전", "부산"])),
            get_options=Mock(return_value=["child"]),
        )
        render = NavigationRender(event_generator=events, allow_back=True)
        with patch("terminal_input.NavigationRender", return_value=render), \
                patch("reservation_guard.main") as runner, \
                contextlib.redirect_stdout(io.StringIO()):
            configure_reservation(app)
            app.reserve()
        runner.assert_called_once()
        args = runner.call_args.args[0]
        self.assertEqual(args[args.index("--arr") + 1], "대전")
        self.assertEqual(args[args.index("--children") + 1], "2")
        self.assertEqual(args[args.index("--interval") + 1], "15")
        self.assertEqual(args[args.index("--max-interval") + 1], "30")
        self.assertIn("--reserve", args)
        self.assertNotIn("--pay", args)

    def test_reservation_escape_does_not_start_runner(self):
        app = SimpleNamespace(
            inquirer=NavigationInquirer(inquirer),
            get_station=Mock(return_value=([], ["서울", "부산"])),
            get_options=Mock(return_value=[]),
        )
        render = NavigationRender(
            event_generator=KeyEventGenerator(key_generator=lambda: key.ESC), allow_back=True,
        )
        with patch("terminal_input.NavigationRender", return_value=render), \
                patch("reservation_guard.main") as runner, \
                contextlib.redirect_stdout(io.StringIO()):
            configure_reservation(app)
            app.reserve()
        runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
