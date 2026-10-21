#!/usr/bin/env python3
"""QQ Music web CLI: song detail, media URLs, verified downloads and ALAC."""

from __future__ import annotations

import argparse
from contextlib import ExitStack
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
from urllib import error, parse, request


SKILL_ROOT = Path(__file__).resolve().parent.parent
COOKIE_NAME = "QQ_MUSIC_COOKIE"
API_URL = "https://u.y.qq.com/cgi-bin/musicu.fcg"
USER_AGENT = "Mozilla/5.0 AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
# These four qualities were tested against the web endpoint. M4A here is AAC.
QUALITIES = {
    "m4a": ("C400", "m4a", "size_96aac"),
    "128": ("M500", "mp3", "size_128mp3"),
    "320": ("M800", "mp3", "size_320mp3"),
    "flac": ("F000", "flac", "size_flac"),
}


class QQMusicWebError(Exception):
    """An error whose message contains no credentials or signed media URLs."""


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def resolve_cookie(env_file=None):
    """Read only QQ_MUSIC_COOKIE: selected .env, then process environment."""
    if env_file is not None:
        path = Path(env_file).expanduser()
    else:
        path = next((p for p in (Path.cwd() / ".env", SKILL_ROOT / ".env") if p.exists()), None)
    cookie = ""
    if path is not None:
        try:
            lines = path.read_text(encoding="utf-8-sig").splitlines()
        except (OSError, UnicodeError) as exc:
            raise QQMusicWebError("无法读取指定的 .env 文件") from exc
        for line in lines:
            match = re.match(r"^\s*(?:export\s+)?QQ_MUSIC_COOKIE\s*=(.*)$", line)
            if not match:
                continue
            value = match.group(1).strip()
            if value.startswith(("'", '"')):
                quoted = re.fullmatch(r"'([^']*)'\s*(?:#.*)?", value)
                if quoted:
                    cookie = quoted.group(1)
                    continue
                quoted = re.fullmatch(r'"((?:[^"\\]|\\.)*)"\s*(?:#.*)?', value)
                if not quoted:
                    raise QQMusicWebError(".env 中 QQ_MUSIC_COOKIE 的引号格式无效")
                cookie = re.sub(r'\\([\\"])', r"\1", quoted.group(1))
            else:
                cookie = re.split(r"\s+#", value, maxsplit=1)[0].strip()
                if cookie.startswith("#"):
                    cookie = ""
    cookie = cookie.strip() or os.environ.get(COOKIE_NAME, "").strip()
    if not cookie:
        raise QQMusicWebError("请在 .env 或环境变量中设置 QQ_MUSIC_COOKIE，使用 QQ 音乐网页登录态")
    if any(ord(c) < 32 or ord(c) > 126 for c in cookie):
        raise QQMusicWebError("Cookie 必须是单行 ASCII 请求头，不能包含控制字符")
    return cookie


def check_code(block, label):
    if not isinstance(block, dict):
        raise QQMusicWebError(label + "回包格式无效")
    if block.get("code") not in (0, "0"):
        code = block.get("code")
        safe_code = str(code) if re.fullmatch(r"-?\d+", str(code)) else "未知"
        raise QQMusicWebError(f"{label}失败（code={safe_code}），请检查登录态和资源权限")


def media_url(value):
    """Use HTTPS Tencent CDN URLs; never forward the web Cookie to the CDN."""
    try:
        parsed = parse.urlsplit(value)
        valid = (parsed.scheme in ("http", "https") and parsed.hostname
                 and parsed.hostname.endswith(".qq.com") and not parsed.username
                 and not parsed.password and parsed.port in (None, 80, 443)
                 and not any(c.isspace() or ord(c) < 32 for c in value))
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise QQMusicWebError("接口返回了无效的媒体地址")
    return parse.urlunsplit(parsed._replace(scheme="https", netloc=parsed.hostname, fragment=""))


class QQMusicWebClient:
    def __init__(self, cookie, timeout=60):
        self._cookie = cookie
        self.timeout = timeout
        self.opener = request.build_opener(NoRedirect())
        fields = dict(part.strip().split("=", 1) for part in cookie.split(";") if "=" in part)
        self._uin = "0"
        for name in ("qqmusic_uin", "uin", "wxuin"):
            match = re.fullmatch(r"o?(\d+)", fields.get(name, ""))
            if match:
                self._uin = match.group(1)
                break
        self._authst = fields.get("qqmusic_key") or fields.get("qm_keyst") or ""

    def api(self, module, method, params):
        body = {"req_0": {"module": module, "method": method, "param": params},
                "comm": {"uin": self._uin, "authst": self._authst,
                         "format": "json", "ct": 19, "cv": 0}}
        req = request.Request(API_URL, data=json.dumps(body).encode("utf-8"), method="POST",
                              headers={"Cookie": self._cookie, "Content-Type": "application/json",
                                       "User-Agent": USER_AGENT, "Referer": "https://y.qq.com/"})
        with self.opener.open(req, timeout=self.timeout) as response:
            raw = response.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            raise QQMusicWebError("接口回包超过大小限制")
        try:
            data = json.loads(raw.decode("utf-8-sig"))
        except (ValueError, UnicodeError) as exc:
            raise QQMusicWebError("接口未返回有效 JSON") from exc
        check_code(data, "网页接口")
        block = data.get("req_0")
        check_code(block, "歌曲接口")
        result = block.get("data")
        if not isinstance(result, dict):
            raise QQMusicWebError("歌曲接口缺少 data 对象")
        return result

    def song_detail(self, mid):
        data = self.api("music.pf_song_detail_svr", "get_song_detail_yqq", {"song_mid": mid})
        track = data.get("track_info")
        if not isinstance(track, dict) or track.get("mid") != mid:
            raise QQMusicWebError("歌曲详情不存在或 MID 不匹配")
        return track

    def song_url(self, track, quality):
        prefix, extension, size_key = QUALITIES[quality]
        files = track.get("file") or {}
        resource = files.get("media_mid")
        if not isinstance(resource, str) or not re.fullmatch(r"[A-Za-z0-9]{14}", resource):
            raise QQMusicWebError("歌曲详情缺少有效 media_mid，不能构造媒体文件名")
        filename = f"{prefix}{resource}.{extension}"
        data = self.api("vkey.GetVkeyServer", "CgiGetVkey", {
            "guid": str(secrets.randbelow(9000000000) + 1000000000),
            "songmid": [track["mid"]], "songtype": [0], "uin": self._uin,
            "loginflag": 1, "platform": "20", "filename": [filename],
        })
        entries = data.get("midurlinfo") or []
        item = next((entry for entry in entries if isinstance(entry, dict)
                     and entry.get("filename") == filename), None)
        if not item or not item.get("purl") or item.get("result", 0) not in (0, "0"):
            code = item.get("result") if item else None
            code = str(code) if re.fullmatch(r"-?\d+", str(code)) else "未知"
            raise QQMusicWebError(f"未获得 {quality} 媒体地址（result={code}），可能受登录态或资源权限限制")
        if item.get("subcode", 0) not in (0, "0"):
            raise QQMusicWebError("媒体文件名与资源不匹配，未使用返回的地址")
        sips = data.get("sip") or []
        sip = next((s for s in sips if isinstance(s, str) and s.startswith("https://")),
                   next((s for s in sips if isinstance(s, str)), ""))
        return {"mid": track["mid"], "quality": quality, "filename": filename,
                "url": media_url(parse.urljoin(sip, item["purl"])),
                "expected_bytes": int(files.get(size_key) or 0)}

    def download_to(self, url, path, expected_bytes, max_bytes):
        if expected_bytes > max_bytes:
            raise QQMusicWebError("歌曲大小超过 --max-size-mb 限制")
        req = request.Request(media_url(url), headers={"User-Agent": USER_AGENT,
                                                      "Referer": "https://y.qq.com/"})
        count = 0
        with self.opener.open(req, timeout=self.timeout) as response, path.open("xb") as output:
            if response.status != 200:
                raise QQMusicWebError("下载未返回完整文件（需要 HTTP 200）")
            try:
                length = int(response.headers.get("Content-Length") or 0)
            except ValueError as exc:
                raise QQMusicWebError("无效的下载长度") from exc
            if length < 0 or length > max_bytes:
                raise QQMusicWebError("下载大小超过 --max-size-mb 限制或长度无效")
            while True:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                count += len(chunk)
                if count > max_bytes:
                    raise QQMusicWebError("下载大小超过 --max-size-mb 限制")
                output.write(chunk)
        if not count or (length and length != count) or (expected_bytes and expected_bytes != count):
            raise QQMusicWebError("下载长度与接口或 HTTP 声明不符，未保留成品")
        return count


def load_av():
    try:
        import av
    except (ImportError, OSError) as exc:
        raise QQMusicWebError("FLAC 校验需要 PyAV：python3 -m pip install av") from exc
    return av


def flac_info(path):
    with path.open("rb") as source:
        header = source.read(42)
    if (len(header) != 42 or header[:4] != b"fLaC" or header[4] & 127
            or int.from_bytes(header[5:8], "big") != 34):
        raise QQMusicWebError("下载内容不是带有效 STREAMINFO 的 FLAC")
    block = header[8:42]
    packed = int.from_bytes(block[10:18], "big")
    info = {"sample_rate": packed >> 44, "channels": ((packed >> 41) & 7) + 1,
            "bits_per_sample": ((packed >> 36) & 31) + 1,
            "total_frames": packed & ((1 << 36) - 1), "streaminfo_md5": block[18:34].hex()}
    if not info["sample_rate"] or not info["total_frames"] or info["bits_per_sample"] not in (16, 24, 32):
        raise QQMusicWebError("FLAC 校验目前支持帧数已知的 16/24/32-bit 文件")
    return info


def pcm_digest(path, info):
    """Hash packed signed PCM, with no resampling, gain, or bit-depth reduction."""
    av = load_av()
    sha, md5, frames = hashlib.sha256(), hashlib.md5(), 0
    bits = info["bits_per_sample"]
    with av.open(str(path)) as container:
        stream = container.streams.audio[0]
        rate, channels = stream.codec_context.sample_rate, len(stream.codec_context.layout.channels)
        if rate != info["sample_rate"] or channels != info["channels"]:
            raise QQMusicWebError("解码后的采样率或声道数不匹配")
        converter = av.AudioResampler(format="s16" if bits == 16 else "s32",
                                     layout=stream.codec_context.layout, rate=rate)

        def consume(frame):
            nonlocal frames
            raw = bytes(frame.planes[0])[:frame.samples * channels * (2 if bits == 16 else 4)]
            sha.update(raw)
            if bits == 24:
                packed = bytearray(len(raw) // 4 * 3)
                packed[0::3], packed[1::3], packed[2::3] = raw[1::4], raw[2::4], raw[3::4]
                md5.update(packed)
            else:
                md5.update(raw)
            frames += frame.samples

        for frame in container.decode(audio=0):
            for converted in converter.resample(frame):
                consume(converted)
        for converted in converter.resample(None):
            consume(converted)
        codec = stream.codec_context.name
    if frames != info["total_frames"]:
        raise QQMusicWebError("音频解码后的有效帧数不匹配")
    return {"codec": codec, "frames": frames, "pcm_sha256": sha.hexdigest(), "pcm_md5": md5.hexdigest()}


def validate_flac(path, info):
    digest = pcm_digest(path, info)
    if digest["codec"] != "flac":
        raise QQMusicWebError("解码器识别的格式不是 FLAC")
    if info["streaminfo_md5"] != "0" * 32 and digest["pcm_md5"] != info["streaminfo_md5"]:
        raise QQMusicWebError("完整音频与 FLAC 内置 MD5 不一致")
    return digest


def prepare_flac(source, clean):
    """Repair only a CRC-delimited trailer whose removal passes the original MD5."""
    av = load_av()
    info = flac_info(source)
    try:
        return source, info, validate_flac(source, info), 0
    except (av.error.FFmpegError, QQMusicWebError):
        pass
    if info["streaminfo_md5"] == "0" * 32:
        raise QQMusicWebError("FLAC 解码失败且没有内置 MD5，不能安全处理尾部数据")
    last = None
    with av.open(str(source)) as container:
        for packet in container.demux(audio=0):
            if packet.size:
                last = packet
    if last is None or last.pos is None or last.pos < 42:
        raise QQMusicWebError("FLAC 解码失败，无法定位最后一帧")
    data = bytes(last)
    if len(data) > 1024 * 1024 or last.pos + len(data) != source.stat().st_size:
        raise QQMusicWebError("FLAC 尾部结构异常，未修改下载内容")
    crc, ends = 0, []
    for end, value in enumerate(data, 1):
        crc ^= value << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x8005) & 0xffff if crc & 0x8000 else (crc << 1) & 0xffff
        if crc == 0 and end >= 16 and 0 < len(data) - end <= 4096:
            ends.append(end)
    if not ends:
        raise QQMusicWebError("FLAC 校验失败，未找到可验证的尾部附加数据边界")
    keep = last.pos + ends[-1]
    with source.open("rb") as src, clean.open("xb") as dst:
        remaining = keep
        while remaining:
            chunk = src.read(min(remaining, 1024 * 1024))
            if not chunk:
                raise QQMusicWebError("复制音频时意外到达文件末尾")
            dst.write(chunk)
            remaining -= len(chunk)
    try:
        digest = validate_flac(clean, info)
    except av.error.FFmpegError as exc:
        raise QQMusicWebError("处理尾部后仍无法完整解码，未保留成品") from exc
    return clean, info, digest, source.stat().st_size - keep


def convert_alac(source, output, info, source_digest):
    converter = shutil.which("afconvert")
    if not converter:
        raise QQMusicWebError("ALAC 转码目前使用 macOS 自带 afconvert，当前系统未找到该工具")
    if output.exists():
        raise QQMusicWebError("转码临时文件已存在，请先检查上次任务")
    flags = {16: 1, 24: 3, 32: 4}
    command = [converter, "-f", "m4af", "-d", f"alac/{flags[info['bits_per_sample']]}",
               str(source), str(output)]
    result = subprocess.run(command, capture_output=True, timeout=600)
    if result.returncode:
        raise QQMusicWebError(f"afconvert 转码失败（退出码 {result.returncode}），未保留成品")
    digest = pcm_digest(output, info)
    if (digest["codec"] != "alac" or digest["pcm_sha256"] != source_digest["pcm_sha256"]
            or digest["pcm_md5"] != source_digest["pcm_md5"]):
        raise QQMusicWebError("ALAC 与源音频的完整 PCM 校验不一致，未保留成品")
    return digest


def filename_for(track, extension):
    singers = ", ".join(s.get("name", "") for s in track.get("singer", []) if isinstance(s, dict))
    name = f"{singers or '未知歌手'} - {track.get('name') or track['mid']}"
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", name).strip(" .")
    while len(name.encode("utf-8")) > 180:
        name = name[:-1]
    return f"{name} [{track['mid']}].{extension}"


def download_song(client, track, quality, target, alac=False, max_bytes=500 * 1024 * 1024):
    target = Path(target).expanduser().absolute()
    paths = [target.with_name(target.name + suffix)
             for suffix in (".download.part", ".clean.part", ".alac.part")]
    if any(p.exists() or p.is_symlink() for p in [target, *paths]):
        raise QQMusicWebError("目标文件或临时文件已存在，未覆盖任何文件")
    if quality == "flac":
        load_av()
    if alac and not shutil.which("afconvert"):
        raise QQMusicWebError("ALAC 转码需要 macOS 自带 afconvert")
    media = client.song_url(track, quality)
    target.parent.mkdir(parents=True, exist_ok=True)
    # A lock prevents another invocation from owning these same stable scratch paths.
    lock = target.with_name(target.name + ".lock")
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise QQMusicWebError("相同目标的下载任务正在运行，或上次留下了锁文件") from exc
    os.close(descriptor)
    with ExitStack() as cleanup:
        cleanup.callback(lock.unlink, missing_ok=True)
        # Recheck after acquiring the lock; never remove files from an older run.
        if any(p.exists() or p.is_symlink() for p in [target, *paths]):
            raise QQMusicWebError("目标文件或临时文件已存在，未覆盖任何文件")
        for path in paths:
            cleanup.callback(path.unlink, missing_ok=True)
        count = client.download_to(media["url"], paths[0], media["expected_bytes"], max_bytes)
        candidate, details = paths[0], {}
        if quality == "flac":
            candidate, info, digest, removed = prepare_flac(paths[0], paths[1])
            details = {**info, **digest, "removed_trailing_bytes": removed,
                       "streaminfo_md5_verified": info["streaminfo_md5"] != "0" * 32}
            if alac:
                digest = convert_alac(candidate, paths[2], info, digest)
                candidate = paths[2]
                details.update(digest)
        # Same-directory hard-link publication is atomic and fails if target exists.
        os.link(candidate, target)
        return {"mid": track["mid"], "name": track.get("name"), "quality": quality,
                "format": "alac" if alac else QUALITIES[quality][1], "path": str(target),
                "download_bytes": count, "file_bytes": target.stat().st_size,
                "verification": details}


def song_mid(value):
    if not re.fullmatch(r"[A-Za-z0-9]{14}", value):
        raise argparse.ArgumentTypeError("请输入 14 位歌曲 MID，可从官方搜索结果的 songMid 获取")
    return value


def positive_number(value):
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("请输入大于 0 的数字") from exc
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("请输入大于 0 的有限数字")
    return number


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--env-file", default=argparse.SUPPRESS, help="指定 .env 文件")
    common.add_argument("--timeout", type=positive_number, default=argparse.SUPPRESS,
                        help="网络读写超时秒数，默认 60")
    parser = argparse.ArgumentParser(description=__doc__, parents=[common])
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (("song-detail", "歌曲详情"), ("song-url", "获取有时效的媒体链接"),
                            ("download", "下载歌曲，可选转码为 M4A / ALAC")):
        child = commands.add_parser(name, help=help_text, parents=[common])
        child.add_argument("mid", type=song_mid)
        if name != "song-detail":
            child.add_argument("--quality", choices=QUALITIES, default="flac", help="默认 flac；m4a 为 AAC")
        if name == "download":
            destination = child.add_mutually_exclusive_group()
            destination.add_argument("--output", type=Path, help="完整目标文件路径，不覆盖已有文件")
            destination.add_argument("--output-dir", type=Path, help="输出目录，默认 skill/.noindex/downloads")
            child.add_argument("--alac", action="store_true", help="FLAC 无损转 M4A / ALAC，需要 macOS 和 PyAV")
            child.add_argument("--max-size-mb", type=positive_number, default=500, help="下载大小上限，默认 500 MiB")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "download" and args.alac and args.quality != "flac":
        parser.error("--alac 必须与 --quality flac 一起使用")
    try:
        client = QQMusicWebClient(resolve_cookie(getattr(args, "env_file", None)),
                                  getattr(args, "timeout", 60))
        track = client.song_detail(args.mid)
        if args.command == "song-detail":
            result = track
        elif args.command == "song-url":
            result = client.song_url(track, args.quality)
        else:
            extension = "m4a" if args.alac else QUALITIES[args.quality][1]
            target = args.output or (args.output_dir or SKILL_ROOT / ".noindex" / "downloads") / filename_for(track, extension)
            if target.suffix.lower() != "." + extension:
                raise QQMusicWebError(f"目标文件扩展名必须是 .{extension}")
            result = download_song(client, track, args.quality, target, args.alac,
                                   int(args.max_size_mb * 1024 * 1024))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except QQMusicWebError as exc:
        message = str(exc)
    except error.HTTPError as exc:
        message = f"HTTP {exc.code}，未转发重定向；请检查登录态、网络或资源权限"
        exc.close()
    except (OSError, http.client.HTTPException, ValueError, subprocess.SubprocessError):
        message = "网络、文件或音频处理失败，未输出凭据或媒体签名；请检查网络、目录和本地音频工具"
    except KeyboardInterrupt:
        return 130
    except Exception:
        # Decoder errors can include filenames; never echo external request data.
        message = "音频解码或回包处理失败，未保留未通过校验的成品"
    print("错误：" + message, file=sys.stderr)
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(0)
