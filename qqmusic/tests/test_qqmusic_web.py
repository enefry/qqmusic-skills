"""Offline regression checks for web downloads, FLAC integrity and native ALAC."""

import contextlib
from email.message import Message
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import struct
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import qqmusic_web as web

try:
    import av
except ImportError:
    av = None


MID = "0039MnYb0qxYhV"
MEDIA_MID = "003Qui1q2u1Zho"
COOKIE = "qqmusic_uin=123456; qqmusic_key=test-private-token"
URL = "https://ws.stream.qqmusic.qq.com/test.flac?vkey=test-private-signature"
TRACK = {"mid": MID, "name": "测试歌曲", "singer": [{"name": "歌手"}],
         "file": {"media_mid": MEDIA_MID, "size_flac": 100}}


class Response(io.BytesIO):
    def __init__(self, body, length=None, status=200):
        super().__init__(body)
        self.status = status
        self.headers = Message()
        if length is not None:
            self.headers["Content-Length"] = str(length)


class ScratchTests(unittest.TestCase):
    def setUp(self):
        scratch = web.SKILL_ROOT / ".noindex" / "tmp"
        scratch.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="web-tests-", dir=scratch)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)


class CookieTests(ScratchTests):
    def setUp(self):
        super().setUp()
        self.cwd, self.skill = self.root / "cwd", self.root / "skill"
        self.cwd.mkdir()
        self.skill.mkdir()
        self.addCleanup(patch.stopall)
        patch.object(web.Path, "cwd", return_value=self.cwd).start()
        patch.object(web, "SKILL_ROOT", self.skill).start()
        patch.dict(os.environ, {web.COOKIE_NAME: COOKIE}, clear=True).start()

    def test_selected_file_precedence_and_empty_fallback(self):
        (self.skill / ".env").write_text("QQ_MUSIC_COOKIE='uin=1; qm_keyst=skill'\n")
        self.assertEqual(web.resolve_cookie(), "uin=1; qm_keyst=skill")
        (self.cwd / ".env").write_text('export QQ_MUSIC_COOKIE="uin=2; qm_keyst=cwd" # comment\n')
        self.assertEqual(web.resolve_cookie(), "uin=2; qm_keyst=cwd")
        custom = self.root / "custom.env"
        custom.write_text("QQ_MUSIC_COOKIE='uin=3; qm_keyst=explicit'\n")
        self.assertEqual(web.resolve_cookie(custom), "uin=3; qm_keyst=explicit")
        (self.cwd / ".env").write_text("OTHER_KEY='unclosed\nQQ_MUSIC_COOKIE=\n")
        self.assertEqual(web.resolve_cookie(), COOKIE)

    def test_values_are_literal_and_quoted_comments_are_preserved(self):
        path = self.cwd / ".env"
        for content, expected in (
            ("\ufeffQQ_MUSIC_COOKIE='uin=1; token=${LITERAL}#hash'", "uin=1; token=${LITERAL}#hash"),
            (r"QQ_MUSIC_COOKIE='token=\"literal\"'", r'token=\"literal\"'),
            ('QQ_MUSIC_COOKIE="token=a#hash" # comment', 'token=a#hash'),
        ):
            with self.subTest(content=content):
                path.write_text(content, encoding="utf-8")
                self.assertEqual(web.resolve_cookie(), expected)

    def test_invalid_cookie_does_not_appear_in_errors(self):
        path = self.cwd / ".env"
        for content in ("QQ_MUSIC_COOKIE='private-unclosed", "QQ_MUSIC_COOKIE=private-密钥"):
            path.write_text(content, encoding="utf-8")
            with self.assertRaises(web.QQMusicWebError) as caught:
                web.resolve_cookie()
            self.assertNotIn("private-", str(caught.exception))
        with self.assertRaisesRegex(web.QQMusicWebError, "无法读取"):
            web.resolve_cookie(self.root / "missing.env")
        path.unlink()
        os.environ[web.COOKIE_NAME] = "token=private\r\nInjected: header"
        with self.assertRaisesRegex(web.QQMusicWebError, "单行"):
            web.resolve_cookie()


class ApiTests(unittest.TestCase):
    def test_post_cookie_and_nested_business_errors(self):
        client = web.QQMusicWebClient(COOKIE)
        client.opener = Mock()
        for payload in ({"code": 104009}, {"code": 0, "req_0": {"code": 104003}}):
            payload["message"] = COOKIE + URL
            client.opener.open.return_value = Response(json.dumps(payload).encode())
            with self.subTest(payload=payload), self.assertRaises(web.QQMusicWebError) as caught:
                client.api("module", "method", {})
            self.assertNotIn("private", str(caught.exception))
        client.opener.open.return_value = Response(b'{"code":0,"req_0":{"code":0,"data":{}}}')
        self.assertEqual(client.api("module", "method", {"test": 1}), {})
        req = client.opener.open.call_args.args[0]
        self.assertEqual(req.full_url, web.API_URL)
        self.assertEqual(req.get_header("Cookie"), COOKIE)
        body = json.loads(req.data)
        self.assertEqual(body["comm"]["uin"], "123456")
        self.assertEqual(body["comm"]["authst"], "test-private-token")
        self.assertEqual(body["req_0"]["param"], {"test": 1})

    def test_vkey_uses_media_mid_once_and_random_guid(self):
        client = web.QQMusicWebClient(COOKIE)
        filename = "F000" + MEDIA_MID + ".flac"
        client.api = Mock(return_value={"sip": ["http://ws.stream.qqmusic.qq.com/"],
                                      "midurlinfo": [{"filename": filename, "purl": "test.flac?vkey=secret",
                                                      "result": 0, "subcode": 0}]})
        with patch.object(web.secrets, "randbelow", side_effect=[2345678901, 3456789012]):
            first = client.song_url(TRACK, "flac")
            first_params = client.api.call_args.args[2]
            client.song_url(TRACK, "flac")
            second_params = client.api.call_args.args[2]
        self.assertEqual(first_params["filename"], [filename])
        self.assertEqual(first_params["songmid"], [MID])
        self.assertNotEqual(first_params["guid"], second_params["guid"])
        self.assertEqual(first["expected_bytes"], 100)
        self.assertTrue(first["url"].startswith("https://"))
        for entry in ({"filename": filename, "result": 104003, "purl": ""},
                      {"filename": filename, "result": 0, "subcode": 1, "purl": "bad"}):
            client.api.return_value = {"midurlinfo": [entry]}
            with self.assertRaises(web.QQMusicWebError):
                client.song_url(TRACK, "flac")

    def test_media_rejects_non_qq_urls_and_redirects(self):
        for value in ("https://qq.com.evil.test/file", "file:///tmp/file", "https://user:pass@a.qq.com/f",
                      "https://a.qq.com:123/f", "https://a.qq.com/\ninjected"):
            with self.subTest(value=value), self.assertRaises(web.QQMusicWebError):
                web.media_url(value)
        self.assertIsNone(web.NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.test/"))


class DownloadTests(ScratchTests):
    def test_complete_download_has_no_cookie_on_cdn_or_leftovers(self):
        client = web.QQMusicWebClient(COOKIE)
        client.song_url = Mock(return_value={"url": URL, "expected_bytes": 10})
        client.opener = Mock()
        client.opener.open.return_value = Response(b"0123456789", 10)
        target = self.root / "song.mp3"
        result = web.download_song(client, TRACK, "320", target)
        self.assertEqual(target.read_bytes(), b"0123456789")
        self.assertEqual(result["download_bytes"], 10)
        self.assertNotIn("url", result)
        req = client.opener.open.call_args.args[0]
        self.assertIsNone(req.get_header("Cookie"))
        self.assertEqual(list(self.root.iterdir()), [target])
        with self.assertRaisesRegex(web.QQMusicWebError, "未覆盖"):
            web.download_song(client, TRACK, "320", target)
        self.assertEqual(target.read_bytes(), b"0123456789")

    def test_partial_size_mismatch_and_interrupt_clean_scratch(self):
        for failure in ("http-length", "metadata-length", "max-size", "interrupt"):
            with self.subTest(failure=failure):
                client = web.QQMusicWebClient(COOKIE)
                client.song_url = Mock(return_value={"url": URL, "expected_bytes": 20 if failure == "metadata-length" else 0})
                client.opener = Mock()
                response = Response(b"0123456789", 20 if failure == "http-length" else None)
                if failure == "interrupt":
                    response.read = Mock(side_effect=[b"partial", KeyboardInterrupt()])
                client.opener.open.return_value = response
                with self.assertRaises(KeyboardInterrupt if failure == "interrupt" else web.QQMusicWebError):
                    web.download_song(client, TRACK, "320", self.root / "song.mp3",
                                      max_bytes=5 if failure == "max-size" else 100)
                self.assertEqual(list(self.root.iterdir()), [])
                self.assertTrue(response.closed)

    def test_preexisting_scratch_and_concurrent_target_are_preserved(self):
        client = Mock()
        target = self.root / "song.mp3"
        part = self.root / "song.mp3.download.part"
        part.write_bytes(b"old work")
        with self.assertRaisesRegex(web.QQMusicWebError, "未覆盖"):
            web.download_song(client, TRACK, "320", target)
        client.song_url.assert_not_called()
        self.assertEqual(part.read_bytes(), b"old work")
        part.unlink()
        client.song_url.return_value = {"url": URL, "expected_bytes": 3}

        def competing_download(url, path, expected_bytes, max_bytes):
            path.write_bytes(b"new")
            target.write_bytes(b"other task")
            return 3

        client.download_to.side_effect = competing_download
        with self.assertRaises(FileExistsError):
            web.download_song(client, TRACK, "320", target)
        self.assertEqual(target.read_bytes(), b"other task")
        self.assertEqual(list(self.root.iterdir()), [target])

    def test_cli_errors_never_echo_transport_secrets(self):
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(web, "resolve_cookie", return_value=COOKIE), \
                patch.object(web.QQMusicWebClient, "song_detail", side_effect=URLError(COOKIE + URL)), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            status = web.main(["download", MID])
        self.assertEqual((status, output.getvalue()), (1, ""))
        self.assertNotIn("private", errors.getvalue())


@unittest.skipUnless(av, "FLAC regressions require optional PyAV")
class AudioTests(ScratchTests):
    def fixture(self, bits=24):
        source = self.root / f"source-{bits}.flac"
        frame = av.AudioFrame(format="s16" if bits == 16 else "s32", layout="stereo", samples=10003)
        frame.sample_rate = 44100
        # A deterministic signal with nonzero low bits at the original bit depth.
        samples = [((i * 123457) % (1 << bits) - (1 << (bits - 1))) << (0 if bits == 16 else 8)
                   for i in range(frame.samples * 2)]
        raw = struct.pack("<" + ("h" if bits == 16 else "i") * len(samples), *samples)
        frame.planes[0].update(raw)
        with av.open(str(source), mode="w", format="flac") as output:
            stream = output.add_stream("flac", rate=44100)
            stream.codec_context.format = frame.format.name
            for packet in stream.encode(frame):
                output.mux(packet)
            for packet in stream.encode(None):
                output.mux(packet)
        return source, hashlib.sha256(raw).hexdigest()

    def test_healthy_flac_keeps_every_sample_and_original_file(self):
        for bits in (16, 24):
            with self.subTest(bits=bits):
                source, expected = self.fixture(bits)
                clean = self.root / "clean.flac"
                candidate, info, digest, removed = web.prepare_flac(source, clean)
                self.assertEqual(candidate, source)
                self.assertEqual((info["bits_per_sample"], digest["frames"], removed), (bits, 10003, 0))
                self.assertEqual(digest["pcm_sha256"], expected)
                self.assertEqual(digest["pcm_md5"], info["streaminfo_md5"])
                self.assertFalse(clean.exists())

    def test_trailers_are_detected_by_frames_and_md5_not_fixed_length(self):
        source, expected = self.fixture()
        raw = source.read_bytes()
        for length in (17, 30, 73, 257):
            with self.subTest(length=length):
                damaged = self.root / f"trailer-{length}.flac"
                damaged.write_bytes(raw + bytes((i * 53 + 2) % 256 for i in range(length)))
                before = damaged.read_bytes()
                candidate, _, digest, removed = web.prepare_flac(damaged, self.root / f"clean-{length}.flac")
                self.assertEqual((removed, digest["pcm_sha256"]), (length, expected))
                self.assertEqual(candidate.read_bytes(), raw)
                self.assertEqual(damaged.read_bytes(), before)

    def test_corrupt_audio_and_md5_fail_without_publishing(self):
        source, _ = self.fixture()
        raw = source.read_bytes()
        with av.open(str(source)) as container:
            first = next(p for p in container.demux(audio=0) if p.size)
            audio_offset = first.pos + first.size // 2
        for offset in (26, audio_offset):
            with self.subTest(offset=offset):
                damaged = bytearray(raw)
                damaged[offset] ^= 0x55
                payload = bytes(damaged) + b"trailer that must not hide corruption"
                client = Mock()
                client.song_url.return_value = {"url": URL, "expected_bytes": len(payload)}

                def download(url, path, expected_bytes, max_bytes):
                    path.write_bytes(payload)
                    return len(payload)

                client.download_to.side_effect = download
                with self.assertRaises((web.QQMusicWebError, av.error.FFmpegError)):
                    web.download_song(client, TRACK, "flac", self.root / "result.flac")
                self.assertEqual(list(self.root.iterdir()), [source])

    @unittest.skipUnless(sys.platform == "darwin" and shutil.which("afconvert"), "ALAC requires macOS afconvert")
    def test_native_alac_roundtrip_preserves_24bit_low_bits(self):
        source, expected = self.fixture()
        info = web.flac_info(source)
        digest = web.validate_flac(source, info)
        target = self.root / "lossless.m4a.alac.part"
        result = web.convert_alac(source, target, info, digest)
        self.assertEqual((result["codec"], result["frames"], result["pcm_sha256"]),
                         ("alac", 10003, expected))
        self.assertEqual(result["pcm_md5"], info["streaminfo_md5"])


if __name__ == "__main__":
    unittest.main()
