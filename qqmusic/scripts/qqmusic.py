#!/usr/bin/env python3
"""QQ Music Agent Gateway CLI (Python 3.9+, standard library only)."""

from __future__ import annotations

import argparse
import http.client
import json
import math
import os
from pathlib import Path
import re
import socket
import sys
from urllib import error, parse, request


SKILL_ROOT = Path(__file__).resolve().parent.parent
SKILL_VERSION = "0.0.3"
KEY_NAME = "QQMUSIC_API_KEY"
DEFAULT_BASE_URL = "https://a.y.qq.com"
SEARCH_TYPES = {
    "song": "0", "album": "1", "playlist": "2",
    "mv": "3", "radio": "4", "singer": "5",
}
PERIODS = {"day": "d", "week": "w", "month": "m", "d": "d", "w": "w", "m": "m"}


class QQMusicError(Exception):
    """A configuration, transport, or gateway error suitable for the CLI."""


def read_env_key(path: Path) -> str | None:
    """Read only QQMUSIC_API_KEY, without executing or expanding .env values."""
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError) as exc:
        raise QQMusicError(f"无法读取 .env 文件 {path}：{exc}") from exc

    result = None
    for number, line in enumerate(lines, 1):
        match = re.match(r"^\s*(?:export\s+)?QQMUSIC_API_KEY\s*=(.*)$", line)
        if not match:
            continue
        value = match.group(1).strip()
        if value.startswith(("'", '"')):
            # Quoted values may contain #. Quotes must close on this line.
            quoted = re.fullmatch(r"'([^']*)'\s*(?:#.*)?", value)
            if quoted:
                value = quoted.group(1)
            else:
                quoted = re.fullmatch(r'"((?:[^"\\]|\\.)*)"\s*(?:#.*)?', value)
                if not quoted:
                    raise QQMusicError(f"{path}:{number} 的 {KEY_NAME} 引号格式无效")
                value = re.sub(r'\\([\\"])', r"\1", quoted.group(1))
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
            if value.startswith("#"):
                value = ""
        result = value.strip() or None
    return result


def validate_key(value: str) -> str:
    value = value.strip()
    if not value or any(character.isspace() or ord(character) < 32 or ord(character) == 127
                        for character in value):
        raise QQMusicError(f"{KEY_NAME} 不能为空或包含空白、控制字符")
    try:
        value.encode("ascii")
    except UnicodeError as exc:
        raise QQMusicError(f"{KEY_NAME} 必须是 ASCII 字符串") from exc
    return value


def resolve_api_key(cli_key: str | None, env_file: str | None) -> str:
    """Precedence: explicit CLI key > selected .env file > process environment."""
    if cli_key is not None:
        return validate_key(cli_key)

    if env_file is not None:
        path = Path(env_file).expanduser()
    else:
        candidates = [Path.cwd() / ".env", SKILL_ROOT / ".env"]
        path = next((candidate for candidate in candidates if candidate.exists()), None)
    if path is not None:
        value = read_env_key(path)
        if value:
            return validate_key(value)

    value = os.environ.get(KEY_NAME, "")
    if value.strip():
        return validate_key(value)
    raise QQMusicError(
        "未找到 API Key。请使用 --api-key、在 .env 中设置 QQMUSIC_API_KEY，"
        "或设置同名环境变量（按此顺序优先）。获取：https://y.qq.com/n/ryqq_v2/qqmusic_skills"
    )


def nonnegative_int(value: str) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("必须是整数") from exc
    if result < 0:
        raise argparse.ArgumentTypeError("不能小于 0")
    return result


def positive_id(value: str) -> int:
    result = nonnegative_int(value)
    if result == 0:
        raise argparse.ArgumentTypeError("ID 必须大于 0")
    return result


def positive_timeout(value: str) -> float:
    try:
        result = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("超时必须是秒数") from exc
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError("超时必须是大于 0 的有限秒数")
    return result


def nonempty_text(value: str) -> str:
    if not value.strip():
        raise argparse.ArgumentTypeError("不能为空")
    return value


def build_parser() -> argparse.ArgumentParser:
    # Suppressed defaults let common options work both before and after commands.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--api-key", "--key", default=argparse.SUPPRESS,
                        help="API Key；优先于 .env 和环境变量")
    common.add_argument("--env-file", default=argparse.SUPPRESS, metavar="PATH",
                        help="指定 .env；默认当前目录 .env，不存在时使用 skill 根目录 .env")
    common.add_argument("--base-url", default=argparse.SUPPRESS, help=f"网关地址，默认 {DEFAULT_BASE_URL}")
    common.add_argument("--timeout", type=positive_timeout, default=argparse.SUPPRESS,
                        metavar="SECONDS", help="网络读写超时，默认 60 秒")
    common.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS,
                        help="只输出 URL 和 JSON 请求体，不读取 Key、不发请求")
    parser = argparse.ArgumentParser(
        description="QQ Music CLI；Key 优先级：--api-key > .env > QQMUSIC_API_KEY 环境变量。",
        parents=[common],
        epilog="普通接口输出 JSON；interpret 实时输出 SSE 原文。通用选项可放在子命令前后。",
    )
    parser.add_argument("--version", action="version", version=SKILL_VERSION)
    commands = parser.add_subparsers(dest="command", required=True)

    def command(name, help_text, path):
        child = commands.add_parser(name, help=help_text, description=help_text, parents=[common])
        child.set_defaults(path=path)
        return child

    search = command("search", "搜索歌曲、专辑、歌单、MV、电台或歌手", "/discover/search")
    search.add_argument("keyword", type=nonempty_text, help="搜索关键词")
    search.add_argument("--type", choices=list(SEARCH_TYPES) + list(SEARCH_TYPES.values()),
                        default="song", help="搜索类型（或 0–5），默认 song")
    search.add_argument("--page", type=nonnegative_int, default=0, help="页码，从 0 开始")
    command("daily-mix", "每日 30 首推荐", "/discover/daily-mix")
    command("radio", "猜你喜欢电台", "/discover/radio")
    command("ai-playlists", "AI 推荐歌单（reqType=all）", "/discover/ai-playlists")
    command("charts", "排行榜分组和榜单摘要", "/charts")
    chart = command("chart-detail", "查看榜单详情", "/charts/detail")
    chart.add_argument("top_id", type=positive_id, help="榜单 topId，从 charts 获取")
    chart.add_argument("--page", type=nonnegative_int, default=0, help="页码，从 0 开始")
    playlist = command("playlist-detail", "查看歌单详情", "/playlists/detail")
    playlist.add_argument("diss_id", type=positive_id, help="歌单 dissId")
    playlist.add_argument("--page", type=nonnegative_int, default=0, help="页码，从 0 开始")
    report = command("report", "听歌日报、周报或月报", "/me/report")
    report.add_argument("--period", "--time-key", choices=list(PERIODS), default="month",
                        help="day/week/month 或 d/w/m，默认 month")
    report.add_argument("--start-time", type=nonnegative_int, metavar="TIMESTAMP",
                        help="Unix 秒；日报目标日零点，周报目标周周一零点，月报忽略")
    interpret = command("interpret", "AI 解读（实时 SSE 流）", "/assistant/ai_interpretation")
    interpret.add_argument("query", type=nonempty_text, help="明确的分析、解读或画像问题")
    interpret.add_argument("--asset-types", type=int, choices=[1, 2], nargs="+",
                           help="1=收藏歌曲，2=听歌记录；默认全选")
    command("check-version", "检查 skill 版本，不自动升级", "/skill/version")
    return parser


def build_params(args: argparse.Namespace) -> dict:
    if args.command == "search":
        return {"keyword": args.keyword, "type": SEARCH_TYPES.get(args.type, args.type), "page": args.page}
    if args.command == "ai-playlists":
        return {"reqType": "all"}
    if args.command == "chart-detail":
        return {"topId": args.top_id, "page": args.page}
    if args.command == "playlist-detail":
        return {"dissId": args.diss_id, "page": args.page}
    if args.command == "report":
        params = {"timeKey": PERIODS[args.period]}
        if args.start_time is not None:
            params["startTime"] = args.start_time
        return params
    if args.command == "interpret":
        params = {"query": args.query}
        if args.asset_types is not None:
            params["assetTypes"] = args.asset_types
        return params
    return {}


def build_url(base_url: str, path: str) -> str:
    try:
        parsed = parse.urlsplit(base_url)
        parsed.port
    except ValueError as exc:
        raise QQMusicError("--base-url 的主机或端口无效") from exc
    if (parsed.scheme not in ("https", "http") or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment
            or any(character.isspace() or ord(character) < 32 for character in base_url)):
        raise QQMusicError("--base-url 必须是无账号、密码、查询参数或 fragment 的 HTTP(S) 地址")
    return base_url.rstrip("/") + path


def check_gateway_error(data) -> None:
    if not isinstance(data, dict):
        raise QQMusicError("接口响应必须是 JSON 对象")
    failures = [f"{name}={data[name]}" for name in ("ret", "sub_ret")
                if data.get(name) not in (None, 0, "0")]
    if failures:
        details = ", ".join(failures)
        message = data.get("msg") or "未提供错误描述"
        trace = f"；traceId={data['traceId']}" if data.get("traceId") else ""
        raise QQMusicError(f"接口错误（{details}）：{message}{trace}")


def decode_json(raw: bytes):
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except (ValueError, UnicodeError) as exc:
        raise QQMusicError("接口未返回有效的 UTF-8 JSON") from exc


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A gateway redirect must not forward the account-bound bearer token.
        return None


def call_api(url: str, key: str, payload: dict, timeout: float, stream: bool) -> None:
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
               "Accept": "text/event-stream, application/json" if stream else "application/json"}
    req = request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                          headers=headers, method="POST")
    try:
        with request.build_opener(NoRedirect()).open(req, timeout=timeout) as response:
            content_type = response.headers.get_content_type()
            if stream and content_type == "text/event-stream":
                # The service documents no event JSON schema: preserve every SSE field.
                for line in response:
                    sys.stdout.write(line.decode("utf-8"))
                    sys.stdout.flush()
            elif content_type == "text/event-stream":
                raise QQMusicError("普通接口意外返回 SSE 流")
            else:
                data = decode_json(response.read())
                check_gateway_error(data)
                if stream:
                    raise QQMusicError("AI 解读未返回 text/event-stream")
                print(json.dumps(data, ensure_ascii=False, indent=2))
    except error.HTTPError as exc:
        try:
            body = exc.read(65536)
        finally:
            exc.close()
        message = ""
        try:
            data = decode_json(body)
            check_gateway_error(data)
            if data.get("msg"):
                message = f"：{data['msg']}"
        except QQMusicError as detail:
            if str(detail).startswith("接口错误"):
                message = f"：{detail}"
        raise QQMusicError(f"HTTP {exc.code}{message}") from exc
    except (socket.timeout, TimeoutError) as exc:
        raise QQMusicError(f"请求超时（{timeout:g} 秒）") from exc
    except error.URLError as exc:
        raise QQMusicError(f"网络请求失败：{exc.reason}") from exc
    except BrokenPipeError:
        raise
    except (OSError, UnicodeError, http.client.HTTPException) as exc:
        raise QQMusicError(f"读取接口响应失败：{exc}") from exc


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    key = None
    try:
        url = build_url(getattr(args, "base_url", DEFAULT_BASE_URL), args.path)
        payload = {"params": build_params(args), "comm": {"skill_version": SKILL_VERSION}}
        if getattr(args, "dry_run", False):
            print(json.dumps({"method": "POST", "url": url, "body": payload}, ensure_ascii=False, indent=2))
            return 0
        key = resolve_api_key(getattr(args, "api_key", None), getattr(args, "env_file", None))
        call_api(url, key, payload, getattr(args, "timeout", 60.0), args.command == "interpret")
        return 0
    except QQMusicError as exc:
        message = str(exc)
        if key:
            message = message.replace(key, "[REDACTED]")
        print(f"错误：{message}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        # Allow piping JSON/SSE to tools such as head without a traceback.
        sys.exit(0)
