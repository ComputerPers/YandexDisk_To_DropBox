import io
import json
import unittest
from http.client import HTTPResponse
from unittest.mock import patch
from urllib.error import HTTPError

from yd2dbx.transport import AuthenticationError, HttpApiError, JsonHttpTransport


def _make_429(retry_after: str = "2") -> HTTPError:
    body = json.dumps({"error": "too_many_requests"}).encode()
    resp = HTTPError(
        url="https://api.dropboxapi.com/2/files/save_url",
        code=429,
        msg="Too Many Requests",
        hdrs={"Retry-After": retry_after},  # type: ignore[arg-type]
        fp=io.BytesIO(body),
    )
    return resp


class TransportTests(unittest.TestCase):
    def test_request_wraps_timeout_as_readable_runtime_error(self) -> None:
        transport = JsonHttpTransport(timeout_seconds=1)

        with patch("yd2dbx.transport.urlopen", side_effect=TimeoutError("The read operation timed out")):
            with self.assertRaises(RuntimeError) as error:
                transport.request("GET", "https://cloud-api.yandex.net/v1/disk")

        self.assertIn("timed out", str(error.exception).lower())
        self.assertIn("https://cloud-api.yandex.net/v1/disk", str(error.exception))

    def test_retries_on_429_then_succeeds(self) -> None:
        waits: list[float] = []
        transport = JsonHttpTransport(
            max_retries_on_rate_limit=3,
            sleep_func=lambda s: waits.append(s),
        )

        ok_body = json.dumps({"status": "ok"}).encode()
        mock_response = io.BytesIO(ok_body)
        mock_response.status = 200  # type: ignore[attr-defined]
        mock_response.headers = {}  # type: ignore[attr-defined]
        mock_response.read = lambda: ok_body  # type: ignore[assignment]

        call_count = 0

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def read(self):
                return ok_body

            def decode(self):
                return ok_body.decode()

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise _make_429("3")
            return FakeResponse()

        with patch("yd2dbx.transport.urlopen", side_effect=side_effect):
            result = transport.request("POST", "https://api.dropboxapi.com/2/files/save_url")

        self.assertEqual(result, {"status": "ok"})
        self.assertEqual(call_count, 3)
        self.assertEqual(waits, [3, 3])

    def test_raises_after_exhausting_429_retries(self) -> None:
        waits: list[float] = []
        transport = JsonHttpTransport(
            max_retries_on_rate_limit=2,
            sleep_func=lambda s: waits.append(s),
        )

        with patch("yd2dbx.transport.urlopen", side_effect=_make_429("1")):
            with self.assertRaises(RuntimeError) as ctx:
                transport.request("POST", "https://api.dropboxapi.com/2/files/save_url")

        self.assertIn("429", str(ctx.exception))
        self.assertEqual(len(waits), 2)


    def test_raises_authentication_error_on_401(self) -> None:
        transport = JsonHttpTransport()

        body = json.dumps({"error": "invalid_access_token"}).encode()
        err = HTTPError(
            url="https://api.dropboxapi.com/2/files/list_folder",
            code=401,
            msg="Unauthorized",
            hdrs={},  # type: ignore[arg-type]
            fp=io.BytesIO(body),
        )

        with patch("yd2dbx.transport.urlopen", side_effect=err):
            with self.assertRaises(AuthenticationError) as ctx:
                transport.request("POST", "https://api.dropboxapi.com/2/files/list_folder")

        self.assertIn("401", str(ctx.exception))
        self.assertIsInstance(ctx.exception, RuntimeError)


    def test_raises_http_api_error_on_409(self) -> None:
        transport = JsonHttpTransport()

        body = json.dumps({"error_summary": "path/conflict/folder"}).encode()
        err = HTTPError(
            url="https://api.dropboxapi.com/2/files/create_folder_v2",
            code=409,
            msg="Conflict",
            hdrs={},  # type: ignore[arg-type]
            fp=io.BytesIO(body),
        )

        with patch("yd2dbx.transport.urlopen", side_effect=err):
            with self.assertRaises(HttpApiError) as ctx:
                transport.request("POST", "https://api.dropboxapi.com/2/files/create_folder_v2")

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIsInstance(ctx.exception, RuntimeError)

    def test_raises_http_api_error_on_500(self) -> None:
        transport = JsonHttpTransport()

        body = json.dumps({"error": "internal"}).encode()
        err = HTTPError(
            url="https://api.dropboxapi.com/2/files/list_folder",
            code=500,
            msg="Internal Server Error",
            hdrs={},  # type: ignore[arg-type]
            fp=io.BytesIO(body),
        )

        with patch("yd2dbx.transport.urlopen", side_effect=err):
            with self.assertRaises(HttpApiError) as ctx:
                transport.request("POST", "https://api.dropboxapi.com/2/files/list_folder")

        self.assertEqual(ctx.exception.status_code, 500)
        self.assertIsInstance(ctx.exception, RuntimeError)


if __name__ == "__main__":
    unittest.main()
