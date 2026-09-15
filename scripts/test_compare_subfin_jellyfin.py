import io
import unittest
import urllib.error
from unittest.mock import Mock, patch

from compare_subfin_jellyfin import HttpClient, JellyfinClient


class UserResolutionTests(unittest.TestCase):
    def setUp(self):
        self.http = Mock(spec=HttpClient)
        self.client = JellyfinClient("https://example.test/prefix/", "test-token", self.http)
        self.user = {"Id": "user-id", "Name": "Alice"}

    def test_session_token_uses_its_user(self):
        self.http.get_json.return_value = self.user
        self.assertEqual(self.client.resolve_user("", "linked-device"), self.user)
        self.assertEqual(self.http.get_json.call_count, 1)

    def test_api_key_resolves_exact_username(self):
        self.http.get_json.side_effect = [
            urllib.error.HTTPError("https://example.test", 400, "Bad Request", {}, None),
            [{"Id": "other", "Name": "Alicia"}, self.user],
        ]
        self.assertEqual(self.client.resolve_user("", "alice"), self.user)

    def test_explicit_id_skips_me(self):
        self.http.get_json.return_value = self.user
        self.assertEqual(self.client.resolve_user("user-id", "device"), self.user)
        self.http.get_json.assert_called_once_with(
            "https://example.test/prefix/Users/user-id", headers={"X-Emby-Token": "test-token"}
        )

    def test_no_matching_user_requires_explicit_id(self):
        self.http.get_json.side_effect = [
            urllib.error.HTTPError("https://example.test", 400, "Bad Request", {}, None),
            [self.user],
        ]
        with self.assertRaisesRegex(RuntimeError, "--jellyfin-user-id"):
            self.client.resolve_user("", "device")

    def test_invalid_token_is_not_treated_as_api_key(self):
        self.http.get_json.side_effect = urllib.error.HTTPError(
            "https://example.test", 401, "Unauthorized", {}, None
        )
        with self.assertRaises(urllib.error.HTTPError):
            self.client.resolve_user("", "alice")
        self.assertEqual(self.http.get_json.call_count, 1)


class RetryTests(unittest.TestCase):
    @patch("compare_subfin_jellyfin.time.sleep")
    @patch("compare_subfin_jellyfin.urllib.request.urlopen")
    def test_bad_request_is_not_retried(self, urlopen, sleep):
        urlopen.side_effect = urllib.error.HTTPError("https://example.test", 400, "Bad Request", {}, None)
        with self.assertRaises(urllib.error.HTTPError):
            HttpClient().get_bytes("https://example.test")
        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()

    @patch("compare_subfin_jellyfin.time.sleep")
    @patch("compare_subfin_jellyfin.urllib.request.urlopen")
    def test_server_failure_is_retried(self, urlopen, sleep):
        urlopen.side_effect = [
            urllib.error.HTTPError("https://example.test", 503, "Unavailable", {}, None),
            io.BytesIO(b"ok"),
        ]
        self.assertEqual(HttpClient().get_bytes("https://example.test"), b"ok")
        sleep.assert_called_once_with(1)


if __name__ == "__main__":
    unittest.main()
