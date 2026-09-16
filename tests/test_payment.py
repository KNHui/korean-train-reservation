import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import payment
from payment import Card, load_card


VALID = {"number": "1234567812345678", "password": "12",
         "birthday": "900101", "expire": "2812"}


class CardTests(unittest.TestCase):
    def test_personal_and_corporate_owner_numbers_choose_the_auth_code(self):
        self.assertEqual(Card(**VALID).auth_type, "J")
        self.assertFalse(Card(**VALID).corporate)
        corporate = Card(**{**VALID, "birthday": "1234567890"})
        self.assertEqual(corporate.auth_type, "S")
        self.assertTrue(corporate.corporate)

    def test_repr_keeps_only_the_last_four_digits(self):
        text = repr(Card(**VALID))
        self.assertIn("****5678", text)
        self.assertNotIn("1234567812345678", text)
        self.assertNotIn(VALID["password"], text)
        self.assertNotIn(VALID["birthday"], text)

    def test_leading_zeros_survive_because_every_field_is_a_string(self):
        card = Card(**{**VALID, "expire": "0412", "password": "04"})
        self.assertEqual(card.expire, "0412")
        self.assertEqual(card.password, "04")

    def test_unusable_values_are_rejected_before_any_charge(self):
        for field, value in (
            ("number", "1234-5678-1234-5678"), ("number", "123"), ("number", ""),
            ("password", "123"), ("password", "ab"),
            ("birthday", "9001"), ("expire", "281"), ("expire", "2813"),
        ):
            with self.subTest(field=field, value=value):
                with self.assertRaises(ValueError):
                    Card(**{**VALID, field: value})
        with self.assertRaises(ValueError):
            Card(**VALID, installment=-1)
        with self.assertRaises(ValueError):
            Card(**VALID, installment=True)


class LoadCardTests(unittest.TestCase):
    def keyring(self, **values):
        stored = {("card", name): value for name, value in values.items()}
        return SimpleNamespace(get_password=Mock(side_effect=lambda service, name:
                                                 stored.get((service, name))))

    def test_stored_card_is_read_from_the_keyring(self):
        with patch.object(payment, "keyring", self.keyring(ok="1", **VALID)):
            self.assertEqual(load_card(emit=Mock()), Card(**VALID))

    def test_missing_incomplete_or_invalid_entries_never_produce_a_card(self):
        emit = Mock()
        for values in ({}, {"ok": "1"}, {"ok": "1", **{**VALID, "number": "abc"}},
                       {**VALID, "ok": None}):
            with self.subTest(values=sorted(values)):
                with patch.object(payment, "keyring", self.keyring(**values)):
                    self.assertIsNone(load_card(emit=emit))

    def test_a_broken_keyring_does_not_raise_into_the_run(self):
        broken = SimpleNamespace(get_password=Mock(side_effect=RuntimeError("locked")))
        with patch.object(payment, "keyring", broken):
            self.assertIsNone(load_card(emit=Mock()))

    def test_messages_never_repeat_a_stored_value(self):
        emit = Mock()
        with patch.object(payment, "keyring",
                          self.keyring(ok="1", **{**VALID, "number": "999"})):
            self.assertIsNone(load_card(emit=emit))
        self.assertNotIn("999", " ".join(str(call) for call in emit.call_args_list))


if __name__ == "__main__":
    unittest.main()
