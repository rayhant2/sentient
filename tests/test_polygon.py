from datetime import datetime, timezone
import json
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from pydantic import SecretStr

from data import polygon
from models.schemas import OHLCVPoint, Ticker


class FakeHTTPResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class FakeHTTPError(HTTPError):
    def __init__(self, code: int, payload):
        super().__init__("https://example.test", code, "error", hdrs=None, fp=None)
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def polygon_payload():
    return {
        "ticker": "NVDA",
        "status": "OK",
        "results": [
            {
                "t": 1_704_067_200_000,
                "o": 100.0,
                "h": 105.0,
                "l": 99.0,
                "c": 104.0,
                "v": 1000,
            },
            {
                "t": 1_704_068_100_000,
                "o": 104.0,
                "h": 108.0,
                "l": 103.0,
                "c": 107.0,
                "v": 1200,
            },
        ],
    }


class PolygonTests(unittest.TestCase):
    def test_get_polygon_api_key_reads_secret_value(self):
        with patch.object(polygon.settings, "polygon_api_key", SecretStr("polygon-secret")):
            self.assertEqual(polygon.get_polygon_api_key(), "polygon-secret")

    def test_get_polygon_api_key_requires_config(self):
        with patch.object(polygon.settings, "polygon_api_key", None):
            with self.assertRaises(polygon.MissingPolygonConfigError):
                polygon.get_polygon_api_key()

    def test_parse_ohlcv_points_maps_polygon_fields_and_sorts(self):
        payload = polygon_payload()
        payload["results"] = list(reversed(payload["results"]))

        points = polygon.parse_ohlcv_points("nvda", payload, limit=2)

        self.assertEqual([point.ticker for point in points], ["NVDA", "NVDA"])
        self.assertEqual([point.close for point in points], [104.0, 107.0])
        self.assertTrue(all(isinstance(point, OHLCVPoint) for point in points))
        self.assertLess(points[0].timestamp, points[1].timestamp)

    def test_parse_ohlcv_points_honors_limit(self):
        points = polygon.parse_ohlcv_points("NVDA", polygon_payload(), limit=1)

        self.assertEqual(len(points), 1)
        self.assertEqual(points[0].close, 107.0)

    def test_parse_ohlcv_points_rejects_missing_fields(self):
        payload = polygon_payload()
        del payload["results"][0]["c"]

        with self.assertRaises(polygon.PolygonError):
            polygon.parse_ohlcv_points("NVDA", payload, limit=2)

    def test_build_aggs_url_uses_expected_endpoint_and_query(self):
        with patch("data.polygon.get_polygon_api_key", return_value="fake-key"):
            url = polygon._build_aggs_url(
                "nvda",
                from_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
                to_date=datetime(2026, 1, 10, tzinfo=timezone.utc),
                limit=150,
            )

        self.assertIn("/v2/aggs/ticker/NVDA/range/15/minute/2026-01-01/2026-01-10", url)
        self.assertIn("adjusted=true", url)
        self.assertIn("sort=asc", url)
        self.assertIn("limit=150", url)
        self.assertIn("apiKey=fake-key", url)

    def test_get_json_returns_decoded_payload(self):
        with patch("data.polygon.urlopen", return_value=FakeHTTPResponse({"status": "OK"})):
            payload = polygon._get_json("https://example.test")

        self.assertEqual(payload, {"status": "OK"})

    def test_get_json_wraps_http_errors(self):
        with patch(
            "data.polygon.urlopen",
            side_effect=FakeHTTPError(403, {"error": "forbidden"}),
        ):
            with self.assertRaises(polygon.PolygonAPIError):
                polygon._get_json("https://example.test")

    def test_get_json_wraps_url_errors(self):
        with patch("data.polygon.urlopen", side_effect=URLError("network down")):
            with self.assertRaises(polygon.PolygonAPIError):
                polygon._get_json("https://example.test")

    def test_fetch_intraday_ohlcv_returns_points(self):
        with patch("data.polygon.get_polygon_api_key", return_value="fake-key"):
            with patch("data.polygon._get_json", return_value=polygon_payload()) as get_json:
                points = polygon.fetch_intraday_ohlcv(
                    "nvda",
                    limit=2,
                    to_date=datetime(2026, 1, 10, tzinfo=timezone.utc),
                    lookback_days=3,
                )

        self.assertEqual(len(points), 2)
        self.assertEqual(points[-1].close, 107.0)
        self.assertEqual(get_json.call_count, 1)

    def test_fetch_intraday_ohlcv_rejects_polygon_error_status(self):
        with patch("data.polygon.get_polygon_api_key", return_value="fake-key"):
            with patch(
                "data.polygon._get_json",
                return_value={"status": "ERROR", "error": "bad request"},
            ):
                with self.assertRaises(polygon.PolygonAPIError):
                    polygon.fetch_intraday_ohlcv("NVDA")

    def test_fetch_intraday_ohlcv_validates_limit_and_lookback(self):
        with self.assertRaises(ValueError):
            polygon.fetch_intraday_ohlcv("NVDA", limit=0)

        with self.assertRaises(ValueError):
            polygon.fetch_intraday_ohlcv("NVDA", lookback_days=0)

    def test_refresh_ticker_data_writes_through_database_layer(self):
        points = polygon.parse_ohlcv_points("NVDA", polygon_payload(), limit=2)

        with patch("data.polygon.fetch_intraday_ohlcv", return_value=points):
            with patch("data.polygon.database.upsert_ticker") as upsert_ticker:
                with patch("data.polygon.database.insert_ticker_data") as insert_ticker_data:
                    with patch("data.polygon.database.delete_old_ticker_data") as delete_old:
                        returned = polygon.refresh_ticker_data("nvda", limit=2)

        self.assertEqual(returned, points)
        self.assertEqual(insert_ticker_data.call_count, 2)
        upserted_ticker = upsert_ticker.call_args.args[0]
        self.assertIsInstance(upserted_ticker, Ticker)
        self.assertEqual(upserted_ticker.ticker, "NVDA")
        self.assertEqual(upserted_ticker.current_price, 107.0)
        delete_old.assert_called_once_with("NVDA", keep=2)

    def test_refresh_ticker_data_does_not_write_when_no_points_returned(self):
        with patch("data.polygon.fetch_intraday_ohlcv", return_value=[]):
            with patch("data.polygon.database.upsert_ticker") as upsert_ticker:
                returned = polygon.refresh_ticker_data("NVDA")

        self.assertEqual(returned, [])
        upsert_ticker.assert_not_called()


if __name__ == "__main__":
    unittest.main()
