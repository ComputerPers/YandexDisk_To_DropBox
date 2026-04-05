import unittest

from yd2dbx.clients.dropbox import DropboxClient
from yd2dbx.clients.yandex_disk import YandexDiskClient
from yd2dbx.transport import AuthenticationError, HttpApiError


class FakeTransport:
    def __init__(self, handler):
        self.handler = handler

    def request(self, method, url, *, headers=None, params=None, json_body=None):
        return self.handler(method, url, headers or {}, params or {}, json_body)


class ClientTests(unittest.TestCase):
    def test_yandex_client_lists_single_page_with_has_more_flag(self) -> None:
        def handler(method, url, headers, params, json_body):
            if url.endswith("/resources/files"):
                self.assertEqual(params["offset"], 1000)
                self.assertEqual(params["limit"], 2)
                return {
                    "items": [
                        {
                            "path": "disk:/docs/c.pdf",
                            "size": 12,
                            "modified": "2024-01-01T10:00:00+00:00",
                            "mime_type": "application/pdf",
                            "md5": "c1",
                            "type": "file",
                        },
                        {
                            "path": "disk:/docs/d.pdf",
                            "size": 13,
                            "modified": "2024-01-01T10:00:00+00:00",
                            "mime_type": "application/pdf",
                            "md5": "d1",
                            "type": "file",
                        },
                    ]
                }
            raise AssertionError(f"Unexpected request: {method} {url}")

        yandex = YandexDiskClient(token="token", transport=FakeTransport(handler))

        entries, has_more = yandex.list_files_page(offset=1000, page_size=2)

        self.assertEqual([entry.path for entry in entries], ["/docs/c.pdf", "/docs/d.pdf"])
        self.assertTrue(has_more)

    def test_yandex_client_lists_page_with_media_type_filter(self) -> None:
        def handler(method, url, headers, params, json_body):
            if url.endswith("/resources/files"):
                self.assertEqual(params["media_type"], "document")
                self.assertEqual(params["offset"], 0)
                return {
                    "items": [
                        {
                            "path": "disk:/docs/a.pdf",
                            "size": 10,
                            "modified": "2024-01-01T10:00:00+00:00",
                            "mime_type": "application/pdf",
                            "md5": "a1",
                            "type": "file",
                        },
                    ]
                }
            raise AssertionError(f"Unexpected request: {method} {url}")

        yandex = YandexDiskClient(token="token", transport=FakeTransport(handler))

        entries, has_more = yandex.list_files_page(offset=0, page_size=100, media_type="document")

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].path, "/docs/a.pdf")
        self.assertFalse(has_more)

    def test_yandex_client_omits_media_type_when_none(self) -> None:
        def handler(method, url, headers, params, json_body):
            if url.endswith("/resources/files"):
                self.assertNotIn("media_type", params)
                return {"items": []}
            raise AssertionError(f"Unexpected request: {method} {url}")

        yandex = YandexDiskClient(token="token", transport=FakeTransport(handler))

        entries, has_more = yandex.list_files_page(offset=0, page_size=100)

        self.assertEqual(entries, [])
        self.assertFalse(has_more)

    def test_yandex_client_lists_files_with_pagination_and_download_url(self) -> None:
        def handler(method, url, headers, params, json_body):
            if url.endswith("/resources/files"):
                offset = int(params["offset"])
                if offset == 0:
                    return {
                        "items": [
                            {
                                "path": "disk:/docs/a.pdf",
                                "size": 10,
                                "modified": "2024-01-01T10:00:00+00:00",
                                "mime_type": "application/pdf",
                                "md5": "a1",
                                "type": "file",
                            }
                        ]
                    }
                return {"items": []}
            if url.endswith("/resources/download"):
                return {"href": "https://download.example/file"}
            raise AssertionError(f"Unexpected request: {method} {url}")

        yandex = YandexDiskClient(token="token", transport=FakeTransport(handler))

        entries = yandex.list_all_files(page_size=1)

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].path, "/docs/a.pdf")
        self.assertEqual(entries[0].source_hash_type, "md5")
        self.assertEqual(yandex.get_download_url("/docs/a.pdf"), "https://download.example/file")

    def test_dropbox_client_lists_start_and_continue_pages(self) -> None:
        def handler(method, url, headers, params, json_body):
            if url.endswith("/files/list_folder"):
                return {
                    "entries": [
                        {
                            ".tag": "file",
                            "path_display": "/Docs/A.pdf",
                            "path_lower": "/docs/a.pdf",
                            "size": 10,
                            "server_modified": "2024-01-01T10:00:00Z",
                            "content_hash": "dbx1",
                        }
                    ],
                    "cursor": "cursor-1",
                    "has_more": True,
                }
            if url.endswith("/files/list_folder/continue"):
                self.assertEqual(json_body["cursor"], "cursor-1")
                return {
                    "entries": [
                        {
                            ".tag": "file",
                            "path_display": "/Docs/B.pdf",
                            "path_lower": "/docs/b.pdf",
                            "size": 11,
                            "server_modified": "2024-01-01T10:01:00Z",
                            "content_hash": "dbx2",
                        }
                    ],
                    "cursor": "cursor-2",
                    "has_more": False,
                }
            raise AssertionError(f"Unexpected request: {method} {url}")

        dropbox = DropboxClient(token="token", transport=FakeTransport(handler))

        first_entries, cursor, has_more = dropbox.list_folder_start("")
        next_entries, next_cursor, next_has_more = dropbox.list_folder_continue("cursor-1")

        self.assertEqual([entry.path for entry in first_entries], ["/Docs/A.pdf"])
        self.assertEqual(cursor, "cursor-1")
        self.assertTrue(has_more)
        self.assertEqual([entry.path for entry in next_entries], ["/Docs/B.pdf"])
        self.assertEqual(next_cursor, "cursor-2")
        self.assertFalse(next_has_more)

    def test_dropbox_client_lists_files_save_url_and_job_status(self) -> None:
        def handler(method, url, headers, params, json_body):
            if url.endswith("/files/list_folder"):
                self.assertTrue(json_body["recursive"])
                return {
                    "entries": [
                        {
                            ".tag": "file",
                            "path_display": "/Docs/A.pdf",
                            "path_lower": "/docs/a.pdf",
                            "size": 10,
                            "server_modified": "2024-01-01T10:00:00Z",
                            "content_hash": "dbx1",
                        }
                    ],
                    "cursor": "cursor-1",
                    "has_more": True,
                }
            if url.endswith("/files/list_folder/continue"):
                self.assertEqual(json_body["cursor"], "cursor-1")
                return {"entries": [], "cursor": "cursor-1", "has_more": False}
            if url.endswith("/files/save_url"):
                return {".tag": "async_job_id", "async_job_id": "job-1"}
            if url.endswith("/files/save_url/check_job_status"):
                return {
                    ".tag": "complete",
                    "metadata": {
                        "name": "A.pdf",
                        "path_display": "/Docs/A.pdf",
                    },
                }
            raise AssertionError(f"Unexpected request: {method} {url}")

        dropbox = DropboxClient(token="token", transport=FakeTransport(handler))

        entries = dropbox.list_all_files("")

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].path, "/Docs/A.pdf")
        self.assertEqual(entries[0].source_hash, "dbx1")

        job_id = dropbox.save_url("/Docs/A.pdf", "https://download.example/file")
        status = dropbox.check_save_url_job("job-1")

        self.assertEqual(job_id, "job-1")
        self.assertEqual(status.tag, "complete")
        self.assertEqual(status.metadata["path_display"], "/Docs/A.pdf")

    def test_dropbox_list_folder_children_returns_files_and_folders(self) -> None:
        def handler(method, url, headers, params, json_body):
            if url.endswith("/files/list_folder"):
                self.assertFalse(json_body["recursive"])
                return {
                    "entries": [
                        {
                            ".tag": "file",
                            "path_display": "/root.txt",
                            "size": 5,
                            "server_modified": "2024-01-01T10:00:00Z",
                            "content_hash": "h1",
                        },
                        {
                            ".tag": "folder",
                            "path_display": "/Documents",
                        },
                        {
                            ".tag": "folder",
                            "path_display": "/Photos",
                        },
                    ],
                    "cursor": "c1",
                    "has_more": False,
                }
            raise AssertionError(f"Unexpected request: {method} {url}")

        dropbox = DropboxClient(token="token", transport=FakeTransport(handler))

        files, folders = dropbox.list_folder_children("")

        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].path, "/root.txt")
        self.assertEqual(sorted(folders), ["/Documents", "/Photos"])

    def test_dropbox_create_folder_batch_returns_none_on_sync_complete(self) -> None:
        def handler(method, url, headers, params, json_body):
            if url.endswith("/create_folder_batch"):
                self.assertEqual(json_body["paths"], ["/a", "/b"])
                return {".tag": "complete", "entries": []}
            raise AssertionError(f"Unexpected: {url}")

        dropbox = DropboxClient(token="t", transport=FakeTransport(handler))
        result = dropbox.create_folder_batch(["/a", "/b"])
        self.assertIsNone(result)

    def test_dropbox_create_folder_batch_returns_job_id_on_async(self) -> None:
        def handler(method, url, headers, params, json_body):
            if url.endswith("/create_folder_batch"):
                return {".tag": "async_job_id", "async_job_id": "job-123"}
            raise AssertionError(f"Unexpected: {url}")

        dropbox = DropboxClient(token="t", transport=FakeTransport(handler))
        result = dropbox.create_folder_batch(["/a"])
        self.assertEqual(result, "job-123")

    def test_dropbox_check_folder_batch_job(self) -> None:
        def handler(method, url, headers, params, json_body):
            if url.endswith("/create_folder_batch/check"):
                self.assertEqual(json_body["async_job_id"], "job-123")
                return {".tag": "complete", "entries": []}
            raise AssertionError(f"Unexpected: {url}")

        dropbox = DropboxClient(token="t", transport=FakeTransport(handler))
        status = dropbox.check_folder_batch_job("job-123")
        self.assertEqual(status, "complete")

    def test_clients_can_perform_non_destructive_access_checks(self) -> None:
        def handler(method, url, headers, params, json_body):
            if url.endswith("/v1/disk"):
                return {"total_space": 1, "used_space": 1}
            if url.endswith("/files/list_folder"):
                return {"entries": [], "cursor": "cursor-1", "has_more": False}
            if url.endswith("/files/create_folder_v2"):
                return {"metadata": {".tag": "folder", "path_display": json_body["path"]}}
            if url.endswith("/files/delete_v2"):
                return {"metadata": {".tag": "deleted", "name": "tmp"}}
            raise AssertionError(f"Unexpected request: {method} {url}")

        transport = FakeTransport(handler)
        yandex = YandexDiskClient(token="token", transport=transport)
        dropbox = DropboxClient(token="token", transport=transport)

        yandex.check_read_access()
        dropbox.check_read_access()
        dropbox.check_write_access()

    def test_dropbox_auto_refreshes_token_on_401(self) -> None:
        call_count = {"n": 0}

        def handler(method, url, headers, params, json_body):
            call_count["n"] += 1
            auth = headers.get("Authorization", "")
            if "expired" in auth:
                raise AuthenticationError("HTTP 401 for https://api.dropboxapi.com/2/files/list_folder: unauthorized")
            if "fresh_token" in auth:
                return {"entries": [], "cursor": "c", "has_more": False}
            raise AssertionError(f"Unexpected auth: {auth}")

        refreshed_tokens: list[str] = []

        import unittest.mock as mock
        refresh_response = {
            "access_token": "fresh_token_abc",
            "token_type": "bearer",
            "expires_in": 14400,
        }

        with mock.patch("yd2dbx.clients.dropbox.urlopen") as mock_urlopen:
            mock_resp = mock.MagicMock()
            mock_resp.read.return_value = __import__("json").dumps(refresh_response).encode()
            mock_resp.__enter__ = mock.MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = mock.MagicMock(return_value=False)
            mock_urlopen.return_value = mock_resp

            dropbox = DropboxClient(
                token="expired_token",
                transport=FakeTransport(handler),
                refresh_token="my_refresh",
                app_key="my_key",
                app_secret="my_secret",
                on_token_refreshed=lambda t: refreshed_tokens.append(t),
            )
            dropbox.check_read_access()

        self.assertEqual(dropbox.token, "fresh_token_abc")
        self.assertEqual(refreshed_tokens, ["fresh_token_abc"])
        self.assertEqual(call_count["n"], 2)

    def test_dropbox_skips_refresh_when_no_credentials(self) -> None:
        def handler(method, url, headers, params, json_body):
            raise AuthenticationError("HTTP 401 for https://api.dropboxapi.com/2/: unauthorized")

        dropbox = DropboxClient(token="bad_token", transport=FakeTransport(handler))

        with self.assertRaises(AuthenticationError) as ctx:
            dropbox.check_read_access()
        self.assertIn("HTTP 401", str(ctx.exception))

    def test_dropbox_proactive_refresh_when_no_access_token(self) -> None:
        def handler(method, url, headers, params, json_body):
            auth = headers.get("Authorization", "")
            if "new_tok" in auth:
                return {"entries": [], "cursor": "c", "has_more": False}
            raise AssertionError(f"Unexpected auth: {auth}")

        import unittest.mock as mock
        refresh_response = {"access_token": "new_tok_123", "expires_in": 14400}

        with mock.patch("yd2dbx.clients.dropbox.urlopen") as mock_urlopen:
            mock_resp = mock.MagicMock()
            mock_resp.read.return_value = __import__("json").dumps(refresh_response).encode()
            mock_resp.__enter__ = mock.MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = mock.MagicMock(return_value=False)
            mock_urlopen.return_value = mock_resp

            dropbox = DropboxClient(
                token="",
                transport=FakeTransport(handler),
                refresh_token="rf",
                app_key="ak",
                app_secret="as",
            )

        self.assertEqual(dropbox.token, "new_tok_123")
        dropbox.check_read_access()


    def test_dropbox_ensure_folders_ignores_409_conflict(self) -> None:
        calls: list[str] = []

        def handler(method, url, headers, params, json_body):
            if url.endswith("/create_folder_v2"):
                path = json_body["path"]
                calls.append(path)
                raise HttpApiError(409, url, '{"error_summary": "path/conflict/folder"}')
            raise AssertionError(f"Unexpected: {url}")

        dropbox = DropboxClient(token="t", transport=FakeTransport(handler))
        dropbox.ensure_folders(["/existing"])
        self.assertEqual(calls, ["/existing"])

    def test_dropbox_ensure_folders_raises_on_500(self) -> None:
        def handler(method, url, headers, params, json_body):
            if url.endswith("/create_folder_v2"):
                raise HttpApiError(500, url, '{"error": "internal"}')
            raise AssertionError(f"Unexpected: {url}")

        dropbox = DropboxClient(token="t", transport=FakeTransport(handler))
        with self.assertRaises(HttpApiError) as ctx:
            dropbox.ensure_folders(["/new_folder"])
        self.assertEqual(ctx.exception.status_code, 500)


if __name__ == "__main__":
    unittest.main()
