"""douyin_transcript —— 本地提取抖音视频文字稿。

用法：
    python douyin_transcript.py <抖音链接或含链接的分享文本> [选项]

流程：解析链接 -> 请求视频详情 -> 优先取平台自动字幕 -> 否则下载音视频
用 faster-whisper 在本地转写 -> 输出 TXT / SRT。

仅依赖 requests 与 faster-whisper；不使用外部 ASR 服务，音频不离开本机。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

try:
    import requests
except ImportError:  # pragma: no cover
    print("缺少 requests 库：pip install requests", file=sys.stderr)
    sys.exit(2)

# 抖音详情接口按 TLS 指纹拦截 requests/python 的握手；curl_cffi 可模拟
# 浏览器指纹通过风控。缺失时回退 requests，可能被 ArgusSecurityPlugin 拦截。
try:
    from curl_cffi import requests as curl_requests
    _HAVE_CURL_CFFI = True
except ImportError:  # pragma: no cover
    curl_requests = None
    _HAVE_CURL_CFFI = False

DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

DETAIL_API = "https://www.douyin.com/aweme/v1/web/aweme/detail/"
TTWID_REGISTER_API = "https://ttwid.bytedance.com/ttwid/union/register/"

# 结果默认输出目录；媒体缓存默认放在工具目录 cache/ 下
def _app_dir() -> Path:
    """打包为 exe 时返回 exe 所在目录，开发时返回本文件所在目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


DEFAULT_OUTPUT_DIR = _app_dir() / "output"
DEFAULT_CACHE_DIR = _app_dir() / "cache"


# ---------------------------------------------------------------- 链接解析

_URL_RE = re.compile(r"https?://[^\s，。,;；'\"<>()（）]+")
# 分享文本里短链后常粘连乱码字符，优先按短链编码精确匹配
_SHORT_LINK_RE = re.compile(r"https?://v\.douyin\.com/[A-Za-z0-9_-]+/?")
_VIDEO_ID_RES = (
    re.compile(r"douyin\.com/(?:video|note)/(\d{10,})"),
    re.compile(r"iesdouyin\.com/share/(?:video|note)/(\d{10,})"),
    re.compile(r"iesdouyin\.com/share/slides/(\d{10,})"),
)


def extract_video_id(text: str, session) -> str:
    """从链接或分享文本中提取视频 ID；短链自动跟随跳转。"""
    match = _SHORT_LINK_RE.search(text) or _URL_RE.search(text)
    if not match:
        raise ValueError(f"输入中找不到抖音链接：{text[:80]!r}")
    url = match.group(0)
    for pattern in _VIDEO_ID_RES:
        m = pattern.search(url)
        if m:
            return m.group(1)
    # 短链（v.douyin.com/xxx 等）：跟随跳转后从最终 URL 提取
    resp = session.get(url, timeout=20, allow_redirects=True)
    for pattern in _VIDEO_ID_RES:
        m = pattern.search(resp.url)
        if m:
            return m.group(1)
    m = re.search(r'"itemId"\s*:\s*"(\d{10,})"', resp.text)
    if m:
        return m.group(1)
    raise ValueError(f"无法从链接解析视频 ID，最终跳转到：{resp.url[:120]}")


# ---------------------------------------------------------------- 详情获取

def build_session(honor_env_proxy: bool = False):
    if _HAVE_CURL_CFFI:
        session = curl_requests.Session(impersonate="chrome124")
        if not honor_env_proxy:
            # libcurl 会读取失效的代理环境变量，显式置空强制直连
            session.proxies = {"http": "", "https": ""}
        return session
    if not honor_env_proxy:
        print("[提示] 未安装 curl_cffi，将使用 requests 直连（可能被抖音风控拦截）。"
              "建议：pip install curl_cffi", file=sys.stderr)
    session = requests.Session()
    session.trust_env = honor_env_proxy
    session.headers.update({"User-Agent": DESKTOP_UA})
    return session


def acquire_ttwid(session: requests.Session) -> None:
    """注册 ttwid Cookie，提高详情接口成功率；失败不致命。"""
    try:
        resp = session.post(
            TTWID_REGISTER_API,
            json={
                "region": "cn",
                "aid": 6383,
                "needFid": False,
                "service": "www.douyin.com",
                "migrate_info": {"ticket": "", "source": "node"},
                "cbUrlProtocol": "https",
                "union": True,
            },
            timeout=15,
        )
        redirect_url = resp.json().get("redirect_url")
        if redirect_url:
            session.get(redirect_url, timeout=15)
    except Exception as exc:  # noqa: BLE001 - requests 与 curl_cffi 异常类型不同，统一忽略
        print(f"[提示] 获取 ttwid 失败（将继续尝试）：{exc}", file=sys.stderr)


def fetch_video_detail(session, video_id: str, retries: int = 4) -> dict:
    """调用网页详情接口，返回 aweme_detail 字典。

    抖音对无签名请求有间歇性软限流（HTTP 200 但响应为空）或 Argus 拦截，
    均按可重试失败处理；重试前刷新 ttwid Cookie 并逐次拉长退避。
    """
    last_error = "未知错误"
    for attempt in range(1, retries + 1):
        if attempt > 1:
            try:
                acquire_ttwid(session)
            except Exception:  # noqa: BLE001 - 刷新失败不影响重试
                pass
        try:
            resp = session.get(
                DETAIL_API,
                params={"aweme_id": video_id, "aid": 6383, "cookie_enabled": "true"},
                headers={"Referer": f"https://www.douyin.com/video/{video_id}"},
                timeout=20,
            )
            text = resp.text
            if resp.status_code == 200 and "aweme_detail" in text:
                detail = resp.json().get("aweme_detail")
                if isinstance(detail, dict):
                    return detail
                last_error = "接口返回中没有 aweme_detail（视频可能已删除或不可见）"
            elif resp.status_code == 200 and not text.strip():
                last_error = "HTTP 200 但响应为空（软限流）"
            elif "ArgusSecurityPlugin" in text or "Uifid" in text:
                last_error = "被风控拦截（ArgusSecurityPlugin）"
            else:
                last_error = f"HTTP {resp.status_code}，响应前 120 字符：{text[:120]!r}"
        except Exception as exc:  # noqa: BLE001 - 网络/解析错误统一重试
            last_error = f"网络请求失败：{exc}"
        if attempt < retries:
            time.sleep(2 * attempt + 1)
    raise RuntimeError(f"获取视频详情失败（已重试 {retries} 次）：{last_error}")


def find_captions(node: object) -> list[dict]:
    """在详情 JSON 中递归查找 caption_infos（平台自动字幕）。"""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("caption_infos", "claInfo", "cla_info") and value:
                candidates = value if isinstance(value, list) else value.get("caption_infos") or []
                if isinstance(candidates, list) and candidates:
                    return candidates
            found = find_captions(value)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = find_captions(item)
            if found:
                return found
    return []


# ---------------------------------------------------------------- 字幕解析

_VTT_TIME = re.compile(r"(\d+):(\d{2}):(\d{2})[.,](\d{3})\s*-->\s*(\d+):(\d{2}):(\d{2})[.,](\d{3})")


def _ts_to_seconds(h, m, s, ms) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def parse_vtt(text: str) -> list[tuple[float, float, str]]:
    """解析 WebVTT/SRT 字幕为 (开始秒, 结束秒, 文本) 列表。"""
    segments: list[tuple[float, float, str]] = []
    lines = text.replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines):
        m = _VTT_TIME.search(lines[i])
        if m:
            start = _ts_to_seconds(m.group(1), m.group(2), m.group(3), m.group(4))
            end = _ts_to_seconds(m.group(5), m.group(6), m.group(7), m.group(8))
            body: list[str] = []
            i += 1
            while i < len(lines) and lines[i].strip() and not _VTT_TIME.search(lines[i]):
                body.append(lines[i].strip())
                i += 1
            content = re.sub(r"<[^>]+>", "", " ".join(body)).strip()
            if content:
                segments.append((start, end, content))
        else:
            i += 1
    return segments


# ---------------------------------------------------------------- 媒体下载

def download_media(session, url: str, dest: Path) -> Path:
    """流式下载视频/音频文件到本地缓存。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    headers = {"Referer": "https://www.douyin.com/"}
    # curl_cffi 的 Response 不支持 with 协议，手动关闭
    resp = session.get(url, headers=headers, stream=True, timeout=(15, 60))
    try:
        if resp.status_code != 200:
            raise RuntimeError(f"媒体下载失败：HTTP {resp.status_code}")
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        last_report = 0.0
        with open(dest, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=256 * 1024):
                fh.write(chunk)
                done += len(chunk)
                now = time.time()
                if now - last_report > 3 and sys.stderr.isatty():
                    pct = f" {done * 100 // total}%" if total else ""
                    print(f"[下载] {done / 1048576:.1f} MB{pct}", file=sys.stderr)
                    last_report = now
    finally:
        resp.close()
    print(f"[下载] 完成：{dest}（{dest.stat().st_size / 1048576:.1f} MB）", file=sys.stderr)
    return dest


# ---------------------------------------------------------------- 本地转写

# ---------------------------------------------------------------- 模型定位

def resolve_model(model_name: str) -> str:
    """模型解析顺序：本地目录 > 随包内置 models/ > HF 名称（走缓存或镜像下载）。"""
    as_path = Path(model_name)
    if as_path.is_dir():
        return str(as_path)
    bundled = _app_dir() / "models" / f"faster-whisper-{model_name}"
    if bundled.is_dir():
        return str(bundled)
    return model_name


def load_whisper_model(model_size: str, device: str, compute_type: str):
    # huggingface.co 直连在国内不可用，默认走镜像站；本地/内置模型不产生网络请求
    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    from faster_whisper import WhisperModel

    resolved = resolve_model(model_size)
    print(f"[转写] 加载模型 {model_size}（{device}/{compute_type}）...", file=sys.stderr)
    return WhisperModel(resolved, device=device, compute_type=compute_type)


def transcribe_audio(
    audio_path: Path,
    model_size: str,
    device: str,
    compute_type: str,
    language: str,
    initial_prompt: str = "",
) -> list[tuple[float, float, str]]:
    model = load_whisper_model(model_size, device, compute_type)
    print("[转写] 开始识别，请稍候（CPU 上约需音频时长的 0.3~1 倍时间）...", file=sys.stderr)
    segments_iter, info = model.transcribe(
        str(audio_path),
        language=None if language == "auto" else language,
        beam_size=5,
        vad_filter=True,
        initial_prompt=initial_prompt[:100] or None,
    )
    segments = []
    for seg in segments_iter:
        text = seg.text.strip()
        if text:
            segments.append((seg.start, seg.end, text))
            print(f"  [{seg.start:7.1f}s] {text}", file=sys.stderr)
    print(f"[转写] 完成，检测语言：{info.language}，共 {len(segments)} 段", file=sys.stderr)
    return segments


# ---------------------------------------------------------------- 输出格式

def merge_paragraphs(segments: list[tuple[float, float, str]], gap: float = 2.5) -> str:
    """按时间间隔把分句合并成自然段落。"""
    paragraphs: list[str] = []
    buffer = ""
    prev_end = None
    for start, end, text in segments:
        if prev_end is not None and start - prev_end > gap and buffer:
            paragraphs.append(buffer)
            buffer = ""
        buffer += text
        prev_end = end
    if buffer:
        paragraphs.append(buffer)
    return "\n\n".join(paragraphs)


def format_srt(segments: list[tuple[float, float, str]]) -> str:
    def ts(seconds: float) -> str:
        ms = int(round(seconds * 1000))
        h, rest = divmod(ms, 3600000)
        m, rest = divmod(rest, 60000)
        s, ms = divmod(rest, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    lines = []
    for idx, (start, end, text) in enumerate(segments, 1):
        lines.append(f"{idx}\n{ts(start)} --> {ts(end)}\n{text}\n")
    return "\n".join(lines)


def read_clipboard() -> str:
    """读取剪贴板文本（Windows）。失败返回空串。"""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "Get-Clipboard"],
            capture_output=True, text=True, encoding="utf-8", timeout=10,
        )
        return (result.stdout or "").strip()
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"[提示] 读取剪贴板失败：{exc}", file=sys.stderr)
        return ""


# ---------------------------------------------------------------- Notion 推送

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
NOTION_CONFIG_FILE = "notion.local.json"  # 放在工具/exe 目录；内容含密钥，不提交 Git
# 页面 ID 可能是 32 位十六进制，也可能是带连字符的 UUID 形式
_NOTION_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_NOTION_HEX32_RE = re.compile(r"[0-9a-f]{32}", re.I)


def _read_env_local(keys: tuple[str, ...]) -> dict[str, str]:
    """从 .env.local 读取指定键（项目既有密钥约定，文件不提交 Git）。"""
    candidates = [
        _app_dir() / ".env.local",              # 打包后放在 exe 旁边
        _app_dir().parents[1] / ".env.local",   # 开发时位于项目根目录
    ]
    values: dict[str, str] = {}
    for path in candidates:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            if key in keys and key not in values:
                values[key] = val.strip().strip('"').strip("'")
    return values


def load_notion_config(args: argparse.Namespace) -> tuple[str, str]:
    """解析 Notion 配置：命令行参数 > 环境变量 > notion.local.json > .env.local。"""
    token = args.notion_token or os.environ.get("NOTION_TOKEN", "")
    parent = args.notion_parent or os.environ.get("NOTION_PARENT", "")

    if not token or not parent:
        from_env = _read_env_local(("NOTION_TOKEN", "NOTION_PARENT"))
        token = token or from_env.get("NOTION_TOKEN", "")
        parent = parent or from_env.get("NOTION_PARENT", "")

    config_path = _app_dir() / NOTION_CONFIG_FILE
    if (not token or not parent) and config_path.is_file():
        import json as _json
        saved = _json.loads(config_path.read_text(encoding="utf-8"))
        token = token or saved.get("token", "")
        parent = parent or saved.get("parent", "")

    if not token or not parent:
        raise RuntimeError(
            "未配置 Notion，四种方式任选：\n"
            "  1) 在项目根目录 .env.local 加两行：NOTION_TOKEN=... / NOTION_PARENT=...\n"
            "  2) 在工具目录建 notion.local.json：{\"token\": \"...\", \"parent\": \"页面链接或ID\"}\n"
            "  3) 环境变量 NOTION_TOKEN / NOTION_PARENT\n"
            "  4) 命令行 --notion-token / --notion-parent\n"
            "拿到 token：https://www.notion.so/my-integrations 新建集成并复制 Secret；\n"
            "然后在目标 Notion 页面右上角 … → 连接 → 添加该集成（否则无权写入）。"
        )
    return token, parent


def parse_notion_page_id(parent: str) -> str:
    """从页面 URL 或裸 ID 提取 32 位页面 ID。

    注意不能先去掉连字符再匹配：URL slug（如 .../My-Page-1a2b...）去掉连字符后
    会把 slug 尾字母并进 ID，导致解析错位。
    """
    m = _NOTION_UUID_RE.search(parent)
    if m:
        return m.group(0).replace("-", "").lower()
    m = _NOTION_HEX32_RE.search(parent)
    if m:
        return m.group(0).lower()
    raise ValueError(f"无法从 {parent!r} 解析 Notion 页面 ID")


def notion_text_blocks(paragraphs: list[str]) -> list[dict]:
    """把文字稿段落转成 Notion 块（单块 ≤2000 字符，遵守 API 限制）。"""
    blocks = []
    for para in paragraphs:
        for i in range(0, max(len(para), 1), 2000):
            chunk = para[i:i + 2000]
            if not chunk.strip():
                continue
            blocks.append({
                "object": "block", "type": "paragraph",
                "paragraph": {"rich_text": [{"text": {"content": chunk}}]},
            })
    return blocks


def push_to_notion(session, args, title, author, video_id, paragraphs) -> str:
    """创建 Notion 子页面，返回页面 URL。"""
    token, parent = load_notion_config(args)
    page_id = parse_notion_page_id(parent)
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }
    meta_lines = [
        f"作者：{author}" if author else None,
        f"来源：https://www.douyin.com/video/{video_id}",
        f"提取时间：{time.strftime('%Y-%m-%d %H:%M')}",
    ]
    children = notion_text_blocks([l for l in meta_lines if l])
    children += [{"object": "block", "type": "divider", "divider": {}}]
    children += notion_text_blocks(paragraphs)

    body = {
        "parent": {"page_id": page_id},
        "icon": {"type": "emoji", "emoji": "🎬"},
        "properties": {"title": {"title": [{"text": {"content": title[:2000]}}]}},
        # 首请求最多带 100 个块，余下的走追加接口
        "children": children[:100],
    }
    resp = session.post(f"{NOTION_API}/pages", headers=headers, json=body, timeout=30)
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Notion 创建页面失败：HTTP {resp.status_code} {resp.text[:300]}")
    created = resp.json()
    page_url = created.get("url", "")
    rest = children[100:]
    while rest:
        batch, rest = rest[:100], rest[100:]
        resp = session.patch(
            f"{NOTION_API}/blocks/{created['id']}/children",
            headers=headers, json={"children": batch}, timeout=30,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Notion 追加内容失败：HTTP {resp.status_code} {resp.text[:300]}")
    return page_url


# ---------------------------------------------------------------- 主流程

def process(url_or_text: str, args: argparse.Namespace) -> Path:
    session = build_session(honor_env_proxy=args.env_proxy)
    video_id = extract_video_id(url_or_text, session)
    print(f"[解析] 视频 ID：{video_id}", file=sys.stderr)

    detail = fetch_video_detail(session, video_id)
    video = detail.get("video") or {}
    title = (detail.get("desc") or video_id).strip()
    author = (detail.get("author") or {}).get("nickname", "")
    duration_ms = video.get("duration") or detail.get("duration") or 0
    print(f"[解析] {title[:50]}  |  作者：{author}  |  时长：{duration_ms / 1000:.0f} 秒", file=sys.stderr)

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    clean_title = re.sub(r"\s+", " ", title).strip().rstrip("# ")
    base_name = re.sub(r'[\\/:*?"<>|]+', "_", clean_title)[:40].rstrip("# ") or video_id
    txt_path = out_dir / f"{base_name}.txt"

    def finish(segments):
        _write_outputs(txt_path, args, segments, title, author, video_id, url_or_text)
        if args.notion:
            page_url = push_to_notion(
                session, args, title=title, author=author, video_id=video_id,
                paragraphs=merge_paragraphs(segments).split("\n\n"),
            )
            print(f"[Notion] 已写入笔记：{page_url}", file=sys.stderr)
        if args.classify:
            try:
                import classify_transcript  # 与本文件同目录
                classify_transcript.classify_file(
                    Path(txt_path), output_dir=Path(args.output).resolve())
            except Exception as exc:  # noqa: BLE001 - 分类失败不影响文稿本身
                print(f"[提示] 自动分类失败（文稿已正常保存）：{exc}", file=sys.stderr)

    # 第一步：尝试平台自动字幕（免转写，最快最准）
    captions = find_captions(detail)
    if captions and not args.force_asr:
        for cap in captions:
            urls = cap.get("url_list") or []
            fmt = (cap.get("format") or "").lower()
            if not urls:
                continue
            try:
                resp = session.get(urls[0], timeout=20)
                if resp.status_code != 200:
                    continue
                segments = parse_vtt(resp.text)
                if segments:
                    print(f"[字幕] 命中平台自动字幕（{fmt or 'webvtt'}），跳过本地转写", file=sys.stderr)
                    finish(segments)
                    return txt_path
            except Exception as exc:  # noqa: BLE001 - 字幕下载失败转本地转写
                print(f"[提示] 字幕下载失败：{exc}", file=sys.stderr)
        print("[字幕] 字幕字段存在但不可用，转本地转写", file=sys.stderr)

    # 第二步：下载媒体并本地转写
    play_addr = video.get("play_addr") or {}
    url_list = play_addr.get("url_list") or []
    if not url_list:
        # 图集（slides）没有音频轨道
        raise RuntimeError("该内容没有可下载的音视频轨道（可能是图集）")
    cache_dir = Path(args.cache_dir)
    media_path = cache_dir / f"{video_id}.mp4"
    if media_path.exists() and media_path.stat().st_size > 0 and not args.no_cache:
        print(f"[下载] 使用缓存文件：{media_path}", file=sys.stderr)
    else:
        download_media(session, url_list[0], media_path)

    segments = transcribe_audio(
        media_path,
        model_size=args.model,
        device=args.device,
        compute_type=args.compute_type,
        language=args.language,
        initial_prompt=title,
    )
    if not segments:
        raise RuntimeError("转写完成但没有识别到任何语音内容（可能是纯音乐/无口播）")
    finish(segments)
    return txt_path


def _write_outputs(txt_path: Path, args, segments, title, author, video_id, source_url) -> None:
    paragraphs = merge_paragraphs(segments)
    header = []
    if args.with_meta:
        header = [
            f"# {title}",
            f"- 作者：{author}" if author else None,
            f"- 视频：https://www.douyin.com/video/{video_id}",
            f"- 提取时间：{time.strftime('%Y-%m-%d %H:%M')}",
        ]
    txt_path.write_text("\n".join([h for h in header if h is not None]) + "\n\n" + paragraphs + "\n", encoding="utf-8")
    if args.srt:
        srt_path = txt_path.with_suffix(".srt")
        srt_path.write_text(format_srt(segments), encoding="utf-8")
        print(f"[输出] {srt_path}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="douyin_transcript",
        description="输入抖音链接，本地提取视频文字稿（平台字幕优先，faster-whisper 兜底）。"
                    "不传链接时自动读取剪贴板。",
    )
    parser.add_argument("url", nargs="?", default=None,
                        help="抖音链接或含链接的分享文本；省略则读剪贴板")
    parser.add_argument("-o", "--output", default=str(DEFAULT_OUTPUT_DIR), help="输出目录（默认 exe/工具目录下的 output）")
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR), help="媒体缓存目录")
    parser.add_argument("--no-cache", action="store_true", help="忽略并覆盖已缓存的媒体文件")
    parser.add_argument("--model", default="small", help="faster-whisper 模型：tiny/base/small/medium/large-v3，或本地模型目录（默认 small）")
    parser.add_argument("--device", default="cpu", help="cpu 或 cuda（默认 cpu）")
    parser.add_argument("--compute-type", default="int8", help="int8/int8_float16/float16/float32（默认 int8）")
    parser.add_argument("--language", default="zh", help="口播语言，auto 为自动检测（默认 zh）")
    parser.add_argument("--srt", action="store_true", help="同时输出带时间轴的 SRT 文件")
    parser.add_argument("--force-asr", action="store_true", help="跳过平台字幕，强制本地转写")
    parser.add_argument("--with-meta", action="store_true", help="TXT 顶部附加标题/作者/链接元信息")
    parser.add_argument("--no-open", action="store_true", help="完成后不自动用记事本打开文字稿")
    parser.add_argument("--notion", action="store_true",
                        help="把文字稿推送到 Notion（需先配置，见 README「Notion 笔记同步」）")
    parser.add_argument("--classify", action="store_true",
                        help="转写完成后调用 MiniMax 自动按内容领域分类归档（见 classify_config.json）")
    parser.add_argument("--notion-token", default=None, help="Notion 集成 Secret（也可用环境变量/配置文件）")
    parser.add_argument("--notion-parent", default=None, help="Notion 父页面链接或 ID（也可用环境变量/配置文件）")
    parser.add_argument("--env-proxy", action="store_true", help="使用系统代理环境变量（默认强制直连）")
    args = parser.parse_args(argv)

    url = args.url
    if not url:
        url = read_clipboard()
        if not url:
            print("[失败] 未传入链接且剪贴板为空。请先复制抖音分享链接，再运行本工具。",
                  file=sys.stderr)
            return 1
        print("[剪贴板] 使用剪贴板内容作为输入", file=sys.stderr)

    started = time.time()
    try:
        txt_path = process(url, args)
    except (ValueError, RuntimeError) as exc:
        print(f"[失败] {exc}", file=sys.stderr)
        if not sys.stdin.isatty() or getattr(sys, "frozen", False):
            # 双击 exe 出错时让窗口停留，用户能看到原因
            try:
                input("\n按回车键退出...")
            except EOFError:
                pass
        return 1
    print(f"[完成] 文字稿已保存：{txt_path}（耗时 {time.time() - started:.0f} 秒）", file=sys.stderr)
    if not args.no_open:
        try:
            os.startfile(txt_path)  # noqa: S606 - Windows 默认程序打开文字稿
        except OSError:
            pass
    print(txt_path.read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
