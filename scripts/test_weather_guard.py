import copy
import datetime as dt
import io
import json
import unittest
import urllib.error
import urllib.parse

import weather_guard as weather


class WeatherGuardTests(unittest.TestCase):
    def setUp(self):
        self.now = dt.datetime(2026, 10, 25, 6, 41, tzinfo=weather.CST)
        self.settings = {"weather": {
            "name": "fixture campus", "latitude_wgs84": 28.5445, "longitude_wgs84": 112.3793,
        }}
        self.payload = {
            "latitude": 28.576448, "longitude": 112.34624,
            "current_units": {"time": "unixtime", "interval": "seconds", "precipitation": "mm",
                              "rain": "mm", "showers": "mm", "weather_code": "wmo code"},
            "current": {"time": int(self.now.timestamp()) - 660, "interval": 900,
                        "precipitation": 0.0, "rain": 0.0, "showers": 0.0, "weather_code": 0},
        }

    def check(self, payload=None):
        value = self.payload if payload is None else payload
        return weather.check_weather(self.settings, now=self.now,
                                     opener=lambda request, timeout: io.BytesIO(json.dumps(value).encode()))

    def test_current_request_uses_configured_coordinates_without_account_credentials(self):
        def opener(request, timeout):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
            self.assertEqual(query["latitude"], ["28.5445"])
            self.assertEqual(query["longitude"], ["112.3793"])
            self.assertEqual(query["current"], ["precipitation,rain,showers,weather_code"])
            self.assertEqual(query["timeformat"], ["unixtime"])
            self.assertEqual(timeout, 15)
            self.assertIsNone(request.get_header("Authorization"))
            self.assertIsNone(request.data)
            return io.BytesIO(json.dumps(self.payload).encode())
        result = weather.check_weather(self.settings, now=self.now, opener=opener)
        self.assertEqual(result["decision"], "dry")
        self.assertEqual(result["interval_seconds"], 900)
        self.assertEqual(result["age_seconds"], 660)

    def test_dry_clouds_and_fog_are_not_rain(self):
        for code in (0, 1, 2, 3, 45, 48):
            with self.subTest(code=code):
                self.payload["current"]["weather_code"] = code
                self.assertEqual(self.check()["decision"], "dry")

    def test_rain_codes_skip_even_when_small_amount_rounds_to_zero(self):
        for code in weather.RAIN_CODES:
            with self.subTest(code=code):
                self.payload["current"]["weather_code"] = code
                result = self.check()
                self.assertEqual(result["decision"], "rain")
                self.assertEqual(result["reason"], "rain_weather_code")

    def test_any_positive_precipitation_overrides_a_clear_weather_code(self):
        for name in weather.AMOUNTS:
            with self.subTest(name=name):
                payload = copy.deepcopy(self.payload)
                payload["current"][name] = 0.01
                result = self.check(payload)
                self.assertEqual(result["decision"], "rain")
                self.assertEqual(result["reason"], "precipitation_detected")

    def test_stale_future_wrong_location_and_wrong_units_are_unavailable(self):
        cases = []
        for delta in (-1801, 301):
            data = copy.deepcopy(self.payload)
            data["current"]["time"] = self.now.timestamp() + delta
            cases.append(data)
        data = copy.deepcopy(self.payload)
        data["latitude"], data["longitude"] = 38.9, 121.5
        cases.append(data)
        data = copy.deepcopy(self.payload)
        data["current_units"]["rain"] = "inch"
        cases.append(data)
        data = copy.deepcopy(self.payload)
        data["current"]["interval"] = 3600
        cases.append(data)
        for data in cases:
            with self.subTest(data=data):
                self.assertEqual(self.check(data)["decision"], "unavailable")

    def test_missing_unknown_and_invalid_values_never_mean_dry(self):
        cases = [{}, {"error": True}, {"hourly": {}}, {"current": None}, []]
        for key, values in (("rain", (None, -1, float("nan"), float("inf"), True)),
                            ("weather_code", (999, None, "rain"))):
            for value in values:
                data = copy.deepcopy(self.payload)
                data["current"][key] = value
                cases.append(data)
        for data in cases:
            with self.subTest(data=data):
                self.assertEqual(self.check(data)["decision"], "unavailable")

    def test_network_failure_malformed_or_oversized_response_is_unavailable(self):
        def fail(request, timeout):
            raise urllib.error.URLError("fixture network failure")
        for opener in (fail, lambda request, timeout: io.BytesIO(b"not json"),
                       lambda request, timeout: io.BytesIO(b" " * (weather.MAX_RESPONSE_BYTES + 1))):
            self.assertEqual(weather.check_weather(self.settings, self.now, opener)["decision"], "unavailable")

    def test_missing_or_invalid_location_does_not_query_weather(self):
        def unexpected_request(*args, **kwargs):
            self.fail("Network request made with missing location")
        for settings in ({}, {"weather": {}}, {"weather": {"latitude_wgs84": 100, "longitude_wgs84": 10}}):
            result = weather.check_weather(settings, self.now, unexpected_request)
            self.assertEqual(result["decision"], "unavailable")


if __name__ == "__main__":
    unittest.main()
