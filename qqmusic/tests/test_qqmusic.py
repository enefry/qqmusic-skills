"""Offline checks: python3 -m unittest discover -s tests -v."""

import contextlib
from email.message import Message
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import qqmusic


class Response(io.BytesIO):
    def __init__(self, body, content_type="application/json"):
        super().__init__(body)
        self.headers = Message()
        self.headers["Content-Type"] = content_type


class KeyTests(unittest.TestCase):
    def setUp(self):
        scratch = qqmusic.SKILL_ROOT / ".noindex" / "tmp"
        scratch.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="qqmusic-tests-", dir=scratch)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cwd = self.root / "cwd"
        self.skill = self.root / "skill"
        self.cwd.mkdir()
        self.skill.mkdir()
        self.addCleanup(patch.stopall)
        patch.object(qqmusic.Path, "cwd", return_value=self.cwd).start()
        patch.object(qqmusic, "SKILL_ROOT", self.skill).start()
        patch.dict(os.environ, {qqmusic.KEY_NAME: "qmk-environment"}, clear=True).start()

    def test_parameter_overrides_dotenv_and_environment(self):
        (self.cwd / ".env").write_text('QQMUSIC_API_KEY="unclosed', encoding="utf-8")
        self.assertEqual(qqmusic.resolve_api_key("qmk-argument", None), "qmk-argument")
        self.assertEqual(qqmusic.resolve_api_key("qmk-argument", "/missing.env"), "qmk-argument")

    def test_dotenv_overrides_environment(self):
        (self.cwd / ".env").write_text('export QQMUSIC_API_KEY="qmk-dotenv" # local\n', encoding="utf-8")
        self.assertEqual(qqmusic.resolve_api_key(None, None), "qmk-dotenv")

    def test_cwd_then_skill_directory_discovery(self):
        (self.skill / ".env").write_text("QQMUSIC_API_KEY=qmk-skill\n", encoding="utf-8")
        self.assertEqual(qqmusic.resolve_api_key(None, None), "qmk-skill")
        (self.cwd / ".env").write_text("QQMUSIC_API_KEY=qmk-cwd\n", encoding="utf-8")
        self.assertEqual(qqmusic.resolve_api_key(None, None), "qmk-cwd")

    def test_explicit_dotenv_replaces_discovery(self):
        explicit = self.root / "custom.env"
        explicit.write_text("QQMUSIC_API_KEY='qmk-explicit'\n", encoding="utf-8")
        (self.cwd / ".env").write_text("QQMUSIC_API_KEY=qmk-cwd\n", encoding="utf-8")
        self.assertEqual(qqmusic.resolve_api_key(None, str(explicit)), "qmk-explicit")

    def test_empty_or_absent_dotenv_key_falls_back_to_environment(self):
        for content in ("", "OTHER_KEY=value", "QQMUSIC_API_KEY=", 'QQMUSIC_API_KEY=""',
                        "QQMUSIC_API_KEY= # empty"):
            with self.subTest(content=content):
                (self.cwd / ".env").write_text(content, encoding="utf-8")
                self.assertEqual(qqmusic.resolve_api_key(None, None), "qmk-environment")

    def test_dotenv_quotes_comments_bom_and_literal_values(self):
        examples = {
            "\ufeffQQMUSIC_API_KEY=qmk-bom": "qmk-bom",
            'QQMUSIC_API_KEY="qmk-quoted#hash" # comment': "qmk-quoted#hash",
            "QQMUSIC_API_KEY='qmk-single#hash'": "qmk-single#hash",
            "QQMUSIC_API_KEY=qmk-unquoted#hash # comment": "qmk-unquoted#hash",
            "OTHER='unclosed\nQQMUSIC_API_KEY=qmk-good": "qmk-good",
            "QQMUSIC_API_KEY=qmk-first\nQQMUSIC_API_KEY=qmk-last": "qmk-last",
            "QQMUSIC_API_KEY=qmk-${OTHER}": "qmk-${OTHER}",
        }
        for content, expected in examples.items():
            with self.subTest(content=content):
                path = self.cwd / ".env"
                path.write_text(content, encoding="utf-8")
                self.assertEqual(qqmusic.resolve_api_key(None, None), expected)

    def test_missing_or_invalid_configuration_is_actionable(self):
        with self.assertRaisesRegex(qqmusic.QQMusicError, "无法读取"):
            qqmusic.resolve_api_key(None, str(self.root / "missing.env"))
        (self.cwd / ".env").write_text("QQMUSIC_API_KEY='unclosed", encoding="utf-8")
        with self.assertRaisesRegex(qqmusic.QQMusicError, "引号格式无效"):
            qqmusic.resolve_api_key(None, None)
        (self.cwd / ".env").unlink()
        os.environ.clear()
        with self.assertRaisesRegex(qqmusic.QQMusicError, "未找到 API Key"):
            qqmusic.resolve_api_key(None, None)
        for value in ("", " ", "qmk-a\nb", "qmk-a b", "qmk-密钥"):
            with self.subTest(value=value), self.assertRaises(qqmusic.QQMusicError):
                qqmusic.resolve_api_key(value, None)


class CliTests(unittest.TestCase):
    def invoke(self, argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = qqmusic.main(argv)
        return status, stdout.getvalue(), stderr.getvalue()

    def test_each_command_builds_documented_request(self):
        examples = [
            (["search", "周杰伦", "--type", "album", "--page", "2"], "/discover/search",
             {"keyword": "周杰伦", "type": "1", "page": 2}),
            (["daily-mix"], "/discover/daily-mix", {}),
            (["radio"], "/discover/radio", {}),
            (["ai-playlists"], "/discover/ai-playlists", {"reqType": "all"}),
            (["charts"], "/charts", {}),
            (["chart-detail", "26", "--page", "1"], "/charts/detail", {"topId": 26, "page": 1}),
            (["playlist-detail", "123456789", "--page", "3"], "/playlists/detail",
             {"dissId": 123456789, "page": 3}),
            (["report"], "/me/report", {"timeKey": "m"}),
            (["report", "--period", "week", "--start-time", "1792368000"], "/me/report",
             {"timeKey": "w", "startTime": 1792368000}),
            (["report", "--time-key", "d"], "/me/report", {"timeKey": "d"}),
            (["interpret", "分析我的听歌风格", "--asset-types", "1", "2"], "/assistant/ai_interpretation",
             {"query": "分析我的听歌风格", "assetTypes": [1, 2]}),
            (["interpret", "分析偏好"], "/assistant/ai_interpretation", {"query": "分析偏好"}),
            (["check-version"], "/skill/version", {}),
        ]
        with patch.object(qqmusic, "resolve_api_key") as resolve, patch.object(qqmusic, "call_api") as call:
            for argv, path, params in examples:
                with self.subTest(argv=argv):
                    status, stdout, stderr = self.invoke(argv + ["--dry-run"])
                    self.assertEqual((status, stderr), (0, ""))
                    self.assertEqual(json.loads(stdout), {
                        "method": "POST", "url": qqmusic.DEFAULT_BASE_URL + path,
                        "body": {"params": params, "comm": {"skill_version": qqmusic.SKILL_VERSION}},
                    })
            resolve.assert_not_called()
            call.assert_not_called()

    def test_all_search_types_and_codes(self):
        for name, code in qqmusic.SEARCH_TYPES.items():
            for value in (name, code):
                with self.subTest(value=value):
                    status, output, _ = self.invoke(["search", "音乐", "--type", value, "--dry-run"])
                    self.assertEqual(status, 0)
                    self.assertEqual(json.loads(output)["body"]["params"]["type"], code)

    def test_common_options_before_and_after_command(self):
        for argv in (
            ["--api-key", "qmk-cli", "--timeout", "3", "search", "晴天"],
            ["search", "晴天", "--key", "qmk-cli", "--timeout", "3"],
            ["--api-key", "qmk-cli", "search", "晴天", "--timeout", "3"],
        ):
            with self.subTest(argv=argv), patch.object(qqmusic, "call_api") as call:
                status, _, stderr = self.invoke(argv)
                self.assertEqual((status, stderr), (0, ""))
                self.assertEqual(call.call_args.args[1], "qmk-cli")
                self.assertEqual(call.call_args.args[3], 3.0)

    def test_invalid_arguments_exit_two(self):
        for argv in (["search", "x", "--page", "-1"], ["chart-detail", "0"],
                     ["report", "--period", "year"], ["interpret", "x", "--asset-types", "3"],
                     ["search", " "], ["--timeout", "nan", "charts"]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit) as caught:
                self.invoke(argv)
            self.assertEqual(caught.exception.code, 2)

    def test_invalid_urls_exit_cleanly(self):
        for url in ("file:///tmp/no", "https://host:bad", "https://[invalid", "https://user:pass@host",
                    "https://host?key=secret", "https://host\n.invalid"):
            with self.subTest(url=url):
                status, output, stderr = self.invoke(["charts", "--base-url", url, "--dry-run"])
                self.assertEqual((status, output), (1, ""))
                self.assertIn("--base-url", stderr)

    def test_gateway_message_redacts_selected_key(self):
        with patch.object(qqmusic, "call_api", side_effect=qqmusic.QQMusicError("bad key: qmk-private")):
            status, output, stderr = self.invoke(["charts", "--api-key", "qmk-private"])
        self.assertEqual((status, output), (1, ""))
        self.assertNotIn("qmk-private", stderr)
        self.assertIn("[REDACTED]", stderr)


class TransportTests(unittest.TestCase):
    def invoke_response(self, response, argv=None):
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(qqmusic.request, "build_opener") as opener:
            if isinstance(response, Exception):
                opener.return_value.open.side_effect = response
            else:
                opener.return_value.open.return_value = response
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                status = qqmusic.main(["--api-key", "qmk-test"] + (argv or ["charts"]))
            req = opener.return_value.open.call_args.args[0]
        return status, output.getvalue(), errors.getvalue(), req

    def test_post_headers_body_and_complete_json_output(self):
        data = {"traceId": "trace", "ret": 0, "sub_ret": "0", "songs": [{"songName": "晴天"}], "hasMore": True}
        response = Response(json.dumps(data).encode())
        status, output, errors, req = self.invoke_response(response, ["search", "晴天"])
        self.assertEqual((status, errors), (0, ""))
        self.assertEqual(json.loads(output), data)
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.get_header("Authorization"), "Bearer qmk-test")
        self.assertEqual(req.get_header("Content-type"), "application/json")
        self.assertEqual(json.loads(req.data), {"params": {"keyword": "晴天", "type": "0", "page": 0},
                                               "comm": {"skill_version": qqmusic.SKILL_VERSION}})
        self.assertTrue(response.closed)

    def test_sse_is_preserved_and_flushed_as_lines_arrive(self):
        class LiveOutput(io.StringIO):
            def __init__(self):
                super().__init__()
                self.snapshots = []

            def flush(self):
                self.snapshots.append(self.getvalue())

        events = ': heartbeat\nevent: delta\ndata: {"text":"音乐"}\n\ndata: [DONE]\n\n'
        response = Response(events.encode(), "text/event-stream; charset=utf-8")
        output = LiveOutput()
        with patch.object(qqmusic.request, "build_opener") as opener, contextlib.redirect_stdout(output):
            opener.return_value.open.return_value = response
            status = qqmusic.main(["interpret", "分析偏好", "--api-key", "qmk-test"])
        self.assertEqual(status, 0)
        self.assertEqual(output.getvalue(), events)
        self.assertEqual(output.snapshots[0], ": heartbeat\n")
        self.assertEqual(output.snapshots[-1], events)
        self.assertEqual(len(output.snapshots), len(events.splitlines()))

    def test_error_responses_fail_without_success_output(self):
        cases = [
            (Response(b'{"ret":403,"msg":"denied","traceId":"abc"}'), "ret=403"),
            (Response(b'{"sub_ret":"9","msg":"denied"}'), "sub_ret=9"),
            (Response(b"not json"), "有效的 UTF-8 JSON"),
            (Response(b"[]"), "JSON 对象"),
            (HTTPError("https://example.invalid", 429, "limited", {},
                       io.BytesIO(b'{"ret":429,"msg":"slow down"}')), "HTTP 429"),
            (URLError("offline"), "网络请求失败"),
            (TimeoutError(), "请求超时"),
        ]
        for response, expected in cases:
            with self.subTest(expected=expected):
                status, output, errors, _ = self.invoke_response(response)
                self.assertEqual((status, output), (1, ""))
                self.assertIn(expected, errors)

    def test_interpret_json_error_and_wrong_content_type(self):
        for body, expected in ((b'{"ret":401,"msg":"expired"}', "ret=401"),
                               (b'{"ret":0}', "未返回 text/event-stream")):
            with self.subTest(body=body):
                status, output, errors, _ = self.invoke_response(Response(body), ["interpret", "分析偏好"])
                self.assertEqual((status, output), (1, ""))
                self.assertIn(expected, errors)

    def test_redirects_do_not_forward_credentials(self):
        handler = qqmusic.NoRedirect()
        req = qqmusic.request.Request("https://example.invalid", headers={"Authorization": "Bearer qmk-test"})
        self.assertIsNone(handler.redirect_request(req, None, 302, "redirect", {}, "https://other.invalid"))


if __name__ == "__main__":
    unittest.main()
