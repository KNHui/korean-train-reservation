import contextlib
import io
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from stations import DEFAULT_STATIONS, STATIONS, merge_stations
from train_cli import configure_stations, main


class StationTests(unittest.TestCase):
    def test_all_original_stations_survive_without_identity_duplicates(self):
        self.assertEqual(len(STATIONS), 45)
        self.assertEqual(STATIONS.count("김천구미"), 1)

    def test_normalization_preserves_order_and_distinct_stations(self):
        self.assertEqual(
            merge_stations([" 김천(구미) ", "김천구미", "", "김천", "창원", "창원중앙"]),
            ("김천구미", "김천", "창원", "창원중앙"),
        )

    def make_app(self, saved=None):
        return SimpleNamespace(
            STATIONS={"KTX": list(STATIONS)},
            DEFAULT_STATIONS={"KTX": ["서울"]},
            get_station=Mock(),
            keyring=SimpleNamespace(get_password=Mock(return_value=saved)),
        )

    def test_korail_choices_include_suseo_dongtan_and_pyeongtaekjije(self):
        app = self.make_app()
        configure_stations(app)
        choices, selected = app.get_station("KTX")
        self.assertEqual(choices, list(STATIONS))
        self.assertEqual(selected, list(DEFAULT_STATIONS))
        self.assertTrue({"수서", "동탄", "평택지제"}.issubset(choices))

    def test_saved_aliases_are_merged_and_custom_stations_preserved(self):
        app = self.make_app("김천(구미), 김천구미, 안동, ,수서")
        configure_stations(app)
        choices, selected = app.get_station("KTX")
        self.assertEqual(selected, ["김천구미", "안동", "수서"])
        self.assertIn("안동", choices)
        self.assertEqual(len(choices), len(set(choices)))

    def test_empty_saved_choices_use_defaults(self):
        app = self.make_app(" , , ")
        configure_stations(app)
        self.assertEqual(app.get_station("KTX")[1], list(DEFAULT_STATIONS))

    def test_listing_does_not_load_client_or_credentials(self):
        output = io.StringIO()
        with patch("train_cli.importlib.import_module") as load, contextlib.redirect_stdout(output):
            main(["--list-stations"])
        load.assert_not_called()
        self.assertEqual(output.getvalue().splitlines(), list(STATIONS))


if __name__ == "__main__":
    unittest.main()
