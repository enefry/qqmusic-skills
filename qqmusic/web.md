# 网页登录态单曲下载

`scripts/qqmusic_web.py` 使用 QQ 音乐网页接口，支持歌曲详情、媒体链接、单曲下载和 FLAC → M4A / ALAC。命令以本 skill 目录为当前目录；也可从任意目录使用脚本绝对路径。

## 配置与依赖

需要 Python 3.9+。在已有 `.env` 中添加 `QQ_MUSIC_COOKIE="完整 Cookie"`，值来自自己登录后的 QQ 音乐网页请求头。不需要 Agent Gateway 的 `QQMUSIC_API_KEY`，不自动读取浏览器 Cookie。

配置优先级：所选 `.env` > 环境变量 `QQ_MUSIC_COOKIE`。可用 `--env-file PATH` 指定文件；默认先找当前目录的 `.env`，不存在时找 skill 根目录的 `.env`，只读取首个存在的文件。文件未配置 Cookie 或值为空时回退到环境变量。仅解析该字段，不执行文件内容、不展开变量。

FLAC 下载需 PyAV，用于完整解码校验；MP3 / AAC 下载与歌曲查询只用标准库：

```bash
python3 -m pip install av
```

ALAC 转码还需 macOS 自带 `afconvert`，不依赖命令行 FFmpeg。不要将真实 Cookie 放进命令文本、日志或版本控制。

## 使用

先通过 `scripts/qqmusic.py search` 获取歌曲的 `songMid`；这里传入 14 位歌曲 MID。

```bash
python3 scripts/qqmusic_web.py song-detail 0039MnYb0qxYhV
python3 scripts/qqmusic_web.py song-url 0039MnYb0qxYhV --quality flac

# 保存 FLAC（默认音质）
python3 scripts/qqmusic_web.py download 0039MnYb0qxYhV --quality flac

# 下载 FLAC 并无损转成 M4A / ALAC
python3 scripts/qqmusic_web.py download 0039MnYb0qxYhV --alac --output-dir /path/to/music

# 指定完整文件名；已有文件会报错，不覆盖
python3 scripts/qqmusic_web.py download 0039MnYb0qxYhV --alac --output /path/to/晴天.m4a
```

| 参数 | 含义 |
| --- | --- |
| `--quality flac/320/128/m4a` | 默认 `flac`；`320`、`128` 是 MP3，`m4a` 是 AAC |
| `--alac` | 仅与 FLAC 一起使用，将完整源音频转为 Apple Lossless，扩展名 `.m4a` |
| `--output-dir DIR` | 下载目录，默认 `skill/.noindex/downloads/` |
| `--output FILE` | 完整目标路径，与 `--output-dir` 互斥；扩展名须匹配输出格式 |
| `--max-size-mb NUMBER` | 单文件下载上限，默认 500 MiB |
| `--env-file PATH` | 指定 Cookie 配置文件，子命令前后均可使用 |
| `--timeout SECONDS` | 单次网络读写超时，默认 60 秒，子命令前后均可使用 |

默认文件名为 `歌手 - 歌名 [MID].扩展名`。加 `--alac` 时只保留最终 ALAC 文件；需另存 FLAC 时单独执行不带 `--alac` 的下载命令。

## 校验和失败处理

下载检查 HTTP 状态、声明长度、歌曲详情中的文件大小和下载上限。FLAC 还检查完整解码、采样率、声道数、有效帧数及源文件内置 PCM MD5。没有内置 MD5 时在 JSON 中标明 `streaminfo_md5_verified: false`；解码失败的此类文件不会自动修复。

部分 FLAC 有导致末帧解码失败的尾部附加数据。脚本仅在定位到 CRC16 边界、附加数据不超过 4096 字节，且清理后的完整音频与原始帧数和内置 MD5 一致时使用清理副本。不会按固定长度裁剪，也不改动已有源文件。

ALAC 转码保留原始采样率、声道数和位深；转码后再次完整解码，对比源音频的 PCM SHA-256、MD5 和有效帧数。目前 FLAC 校验接受帧数已知的 16/24/32-bit 文件；MP3 / AAC 仅检查下载长度，不做完整音频解码。

成功后输出包含最终路径、文件大小和校验结果的 JSON。`download` 输出不包含媒体签名；`song-url` 专门返回短期有效的签名地址，不应分享或持久记录。Cookie 仅发送到固定的 QQ 音乐 API，CDN 下载请求不携带 Cookie，不跟随 HTTP 重定向。

异常或中断会清理本次 `.part` 和锁文件，完成文件通过不覆盖方式发布。进程被强制终止或机器断电可能留下临时文件；确认任务已停止后再检查清理，脚本不会覆盖这些文件。成功退出码为 0，业务/网络/文件/音频错误为 1，参数错误为 2，用户中断为 130。不自动重试或降级音质。能否取得下载链接取决于当前账号登录态和对应资源权限，会员也可能遇到无地址的单曲。

## 回归检查

```bash
python3 -m unittest discover -s tests -v
```

测试不访问网络，使用合成音频验证不同长度的 FLAC 尾部数据、损坏拒绝和 24-bit ALAC PCM 一致性。无 PyAV 时跳过音频测试；ALAC 测试需 macOS 原生音频工具可用。
