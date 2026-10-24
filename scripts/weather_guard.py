"""Check current local precipitation before the daily task can log in or submit."""

import datetime as dt
import json
import math
import urllib.error
import urllib.parse
import urllib.request

from policy_probe import CST

ENDPOINT = "https://api.open-meteo.com/v1/forecast"
MAX_RESPONSE_BYTES = 65536
MAX_AGE_SECONDS = 1800
RAIN_CODES = {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 97, 99}
KNOWN_CODES = RAIN_CODES | {0, 1, 2, 3, 45, 48, 71, 73, 75, 77, 85, 86}
AMOUNTS = ("precipitation", "rain", "showers")


class WeatherDataError(ValueError):
    """A controlled reason that can be written to the public task diagnostics."""


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise WeatherDataError("invalid_numeric_weather_value")
    return value


def reject_nonfinite_json(value):
    raise WeatherDataError("nonfinite_weather_json")


def assess(payload, latitude, longitude, now):
    if not isinstance(payload, dict) or payload.get("error"):
        raise WeatherDataError("weather_provider_error")
    current = payload["current"]
    units = payload["current_units"]
    if units.get("time") != "unixtime" or units.get("interval") != "seconds":
        raise WeatherDataError("unexpected_weather_time_units")
    if any(units.get(name) != "mm" for name in AMOUNTS):
        raise WeatherDataError("unexpected_precipitation_units")
    timestamp = number(current["time"])
    age = now.timestamp() - timestamp
    if age < -300 or age > MAX_AGE_SECONDS:
        raise WeatherDataError("weather_timestamp_out_of_range")
    interval = number(current["interval"])
    if not 0 < interval <= MAX_AGE_SECONDS:
        raise WeatherDataError("unexpected_weather_interval")
    grid_lat = number(payload["latitude"])
    grid_lon = number(payload["longitude"])
    if not -90 <= grid_lat <= 90 or not -180 <= grid_lon <= 180:
        raise WeatherDataError("invalid_weather_grid_location")
    # The provider returns a model grid cell, which can be several kilometres from campus.
    distance_m = math.hypot((grid_lat - latitude) * 111320,
                            (grid_lon - longitude) * 111320 * math.cos(math.radians(latitude)))
    if distance_m > 50000:
        raise WeatherDataError("weather_location_mismatch")
    amounts = {name: number(current[name]) for name in AMOUNTS}
    if any(value < 0 for value in amounts.values()):
        raise WeatherDataError("negative_precipitation")
    code = number(current["weather_code"])
    if code not in KNOWN_CODES:
        raise WeatherDataError("unknown_weather_code")
    wet = any(value > 0 for value in amounts.values())
    rainy_code = code in RAIN_CODES
    return {
        "decision": "rain" if wet or rainy_code else "dry",
        "reason": "precipitation_detected" if wet else "rain_weather_code" if rainy_code else "no_rain_detected",
        "valid_at": dt.datetime.fromtimestamp(timestamp, CST).isoformat(),
        "age_seconds": round(age, 3), "interval_seconds": interval,
        "weather_code": code, "precipitation_mm": amounts,
        "grid_location": {"latitude_wgs84": grid_lat, "longitude_wgs84": grid_lon},
    }


def check_weather(settings, now=None, opener=urllib.request.urlopen):
    now = now or dt.datetime.now(CST)
    report = {
        "source": "Open-Meteo", "source_url": "https://open-meteo.com/en/docs",
        "data_kind": "current_weather_model", "checked_at": now.isoformat(),
        "decision": "unavailable",
    }
    try:
        location = settings["weather"]
        latitude = number(location["latitude_wgs84"])
        longitude = number(location["longitude_wgs84"])
        if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            raise WeatherDataError("invalid_weather_location")
        report["requested_location"] = {
            "name": location.get("name", ""),
            "latitude_wgs84": latitude, "longitude_wgs84": longitude,
        }
        query = urllib.parse.urlencode({
            "latitude": latitude, "longitude": longitude,
            "current": "precipitation,rain,showers,weather_code",
            "timezone": "Asia/Shanghai", "timeformat": "unixtime",
            "precipitation_unit": "mm", "forecast_days": 1,
        })
        request = urllib.request.Request(ENDPOINT + "?" + query, headers={
            "User-Agent": "NekoSportsWorldTool-weather-check/1.0", "Accept": "application/json",
        })
        with opener(request, timeout=15) as response:
            content = response.read(MAX_RESPONSE_BYTES + 1)
        if len(content) > MAX_RESPONSE_BYTES:
            raise WeatherDataError("weather_response_too_large")
        payload = json.loads(content, parse_constant=reject_nonfinite_json)
        report["response"] = payload
        report.update(assess(payload, latitude, longitude, now))
    except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError) as error:
        # Unknown weather must never be interpreted as dry weather.
        report["error_type"] = type(error).__name__
        report["reason"] = str(error) if isinstance(error, WeatherDataError) else "weather_query_or_data_failed"
        if isinstance(error, urllib.error.HTTPError):
            report["http_status"] = error.code
    return report
