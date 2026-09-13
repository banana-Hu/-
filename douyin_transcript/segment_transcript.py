"""segment_transcript —— 用中转站 MiniMax 给抖音文字稿做语义分段。

硬性保证"保持原文"：模型只允许插入换行/空行/“## ”小标题行；输出剥掉标题与
空白后必须与原文逐字相等，否则丢弃结果、保留原文件并记录失败。

用法：
    python segment_transcript.py                    # 递归处理 output 下全部未分段文稿
    python segment_transcript.py --file <路径>       # 处理单篇
    python segment_transcript.py --dry-run          # 只调模型看结果，不写回文件

密钥复用 .env.local 的 MINIMAX_SEARCH_API_KEY / MINIMAX_SEARCH_API_BASE_URL。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

try:
    from curl_cffi import requests as curl_requests  # noqa: F401
    _HAVE_CURL_CFFI = True
except ImportError:  # pragma: no cover
    _HAVE_CURL_CFFI = False

TOOL_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_FILE = TOOL_DIR / "segment_config.json"
DEFAULT_OUTPUT_DIR = TOOL_DIR / "output"
DEFAULT_STATE_NAME = "segment_state.json"
STATE_IGNORE = {"classifications.jsonl"}

ENV_FILE = TOOL_DIR.parents[1] / ".env.local"
ENV_KEY = "MINIMAX_SEARCH_API_KEY"
ENV_BASE_URL = "MINIMAX_SEARCH_API_BASE_URL"
ENV_MODEL = "MINIMAX_SEARCH_MODEL"
DEFAULT_BASE_URL = "https://api.minimaxi.com/anthropic"
DEFAULT_MODEL = "MiniMax-M3"
MESSAGES_PATH = "/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

_SENTENCE_SPLIT = re.compile(r"(?<=[。！？；…])")
_META_LINE = re.compile(r"^(#(?!#) |- )")


# ---------------------------------------------------------------- 配置与环境

def load_local_env() -> None:
    """读取未提交的 .env.local（不覆盖已有环境变量），并清除系统代理直连。"""
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        os.environ.pop(var, None)
    if not ENV_FILE.is_file():
        return
    for raw_line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def load_config(config_file: Path) -> dict:
    config = json.loads(config_file.read_text(encoding="utf-8"))
    if not config.get("prompt"):
        raise RuntimeError(f"分段配置缺少 prompt：{config_file}")
    return config


def resolve_settings(args: argparse.Namespace) -> tuple[str, str, str]:
    load_local_env()
    api_key = os.environ.get(ENV_KEY, "").strip()
    if not api_key:
        raise RuntimeError(f"缺少 {ENV_KEY}。请在 {ENV_FILE} 中配置。")
    base_url = os.environ.get(ENV_BASE_URL, "").strip() or DEFAULT_BASE_URL
    model = os.environ.get(ENV_MODEL, "").strip() or args.model or DEFAULT_MODEL
    return api_key, base_url.rstrip("/"), model


# ---------------------------------------------------------------- 文本处理

def split_header(text: str) -> tuple[list[str], str]:
    """把 --with-meta 产生的头部（“# 标题”+“- 元信息”行）与正文分离。"""
    lines = text.splitlines()
    header: list[str] = []
    for line in lines:
        if _META_LINE.match(line.strip()):
            header.append(line)
        else:
            break
    if header and len(header) < len(lines):
        return header, "\n".join(lines[len(header):]).lstrip("\n")
    return [], text


def parse_srt_timings(srt_path: Path) -> list[tuple[float, float, str]]:
    """从同名 SRT 恢复 (开始, 结束, 文本) 列表；文件不存在返回空表。"""
    if not srt_path.is_file():
        return []
    from douyin_transcript import parse_vtt  # 与主工具同目录，避免循环导入放函数内
    return parse_vtt(srt_path.read_text(encoding="utf-8-sig"))


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text)


def map_paragraph_times(paragraphs: list[str], segments: list[tuple[float, float, str]]) -> list[tuple[float, float] | None]:
    """把模型分段映射回 Whisper 片段的时间区间。

    前提：分段输出通过完整性校验，即所有段落去空白后与原文逐字一致，
    因此可以按字符顺序消费片段。无法对齐的段落返回 None（不标时间）。
    """
    timings: list[tuple[float, float] | None] = []
    index = 0
    for paragraph in paragraphs:
        target = _compact(paragraph)
        if not target:
            timings.append(None)
            continue
        buffer = ""
        start = end = None
        while index < len(segments):
            seg_start, seg_end, seg_text = segments[index]
            if start is None:
                start = seg_start
            buffer += _compact(seg_text)
            end = seg_end
            index += 1
            if len(buffer) >= len(target):
                break
        if buffer == target and start is not None:
            timings.append((start, end))
        else:
            timings.append(None)
    return timings


def _format_clock(seconds: float) -> str:
    total = int(round(seconds))
    minutes, sec = divmod(total, 60)
    return f"{minutes:02d}:{sec:02d}"


def annotate_paragraph_times(segmented: str, timings: list[tuple[float, float] | None]) -> str:
    """给每个非标题段落行首加 [MM:SS-MM:SS] 时间标记。"""
    out_lines: list[str] = []
    index = 0
    for line in segmented.splitlines():
        if line.strip().startswith("##") or not line.strip():
            out_lines.append(line)
            continue
        if index < len(timings) and timings[index]:
            start, end = timings[index]
            out_lines.append(f"[{_format_clock(start)}-{_format_clock(end)}] {line.rstrip()}")
        else:
            out_lines.append(line)
        index += 1
    return "\n".join(out_lines)


def split_sentences(body: str) -> list[str]:
    """按中文句末标点断句，保留标点；空白段丢弃。"""
    sentences = [s.strip() for s in _SENTENCE_SPLIT.split(body) if s.strip()]
    return sentences or ([body.strip()] if body.strip() else [])


def chunk_body(body: str, chunk_chars: int) -> list[str]:
    """把正文按句子边界切成不超过 chunk_chars 的块（长稿分多次调用）。"""
    chunks: list[str] = []
    buffer = ""
    for sentence in split_sentences(body):
        if buffer and len(buffer) + len(sentence) > chunk_chars:
            chunks.append(buffer)
            buffer = ""
        buffer += sentence
    if buffer:
        chunks.append(buffer)
    return chunks


def build_prompt(prompt: str, chunk: str, index: int, total: int) -> str:
    position = f"（这是全文的第 {index}/{total} 部分，只处理本部分）" if total > 1 else ""
    return f"{prompt}{position}\n\n文字稿：\n{chunk}"


def extract_reply_text(payload: dict) -> str:
    blocks = payload.get("content")
    if not isinstance(blocks, list):
        raise RuntimeError(f"MiniMax 响应缺少 content 数组：{str(payload)[:200]}")
    return "".join(
        block.get("text", "") for block in blocks
        if isinstance(block, dict) and block.get("type") == "text"
    ).strip()


def integrity_check(original_body: str, output: str) -> bool:
    """剥掉 “## ” 小标题行与全部空白后，必须与原文逐字相等。"""
    if not output.strip():
        return False
    body_lines = [line for line in output.splitlines()
                  if not line.lstrip().startswith("##")]
    compact = lambda s: re.sub(r"\s+", "", s)  # noqa: E731
    return compact("".join(body_lines)) == compact(original_body)


def call_minimax(session, *, api_key: str, base_url: str, model: str,
                 prompt: str, max_tokens: int, timeout: int = 120) -> str:
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "anthropic-version": ANTHROPIC_VERSION,
    }
    try:
        resp = session.post(f"{base_url}{MESSAGES_PATH}",
                            headers=headers, json=body, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - requests 与 curl_cffi 异常类型不同
        raise RuntimeError(f"请求 MiniMax 失败：{exc}") from exc
    if resp.status_code == 429:
        raise RuntimeError("MiniMax 限流（HTTP 429），请稍后重试")
    if resp.status_code != 200:
        raise RuntimeError(f"MiniMax 返回 HTTP {resp.status_code}：{resp.text[:200]}")
    payload = resp.json()
    base_resp = payload.get("base_resp")
    if isinstance(base_resp, dict) and base_resp.get("status_code") not in (None, 0):
        raise RuntimeError(
            f"MiniMax 业务错误 {base_resp.get('status_code')}：{base_resp.get('status_msg')}")
    reply = extract_reply_text(payload)
    if not reply:
        raise RuntimeError("MiniMax 返回了空回复")
    return reply


# ---------------------------------------------------------------- 单篇处理

_RETRY_WARNING = (
    "\n\n【再次强调】上一次输出因改动了原文被判定无效。请务必逐字保留原文字符，"
    "包括其中的错别字、口语词、英文与空格习惯，一个字都不能改动；"
    "只允许插入换行、空行和“## ”小标题行。"
)


def segment_body(body: str, session, *, api_key: str, base_url: str,
                 config: dict, model: str) -> str:
    """对正文做语义分段；含分块、拼接、完整性校验与失败重试。

    模型偶尔会“顺手改错别字”导致校验失败，这里自动重试
    （config.maxAttempts，默认 3 次）；全部失败抛 RuntimeError，原文件不动。
    """
    chunks = chunk_body(body, int(config.get("chunkChars", 3000)))
    total = len(chunks)
    attempts = max(1, int(config.get("maxAttempts", 3)))
    last_error: str | None = None
    for attempt in range(1, attempts + 1):
        parts: list[str] = []
        for index, chunk in enumerate(chunks, 1):
            prompt = build_prompt(config["prompt"], chunk, index, total)
            if attempt > 1:
                prompt += _RETRY_WARNING
            parts.append(call_minimax(
                session, api_key=api_key, base_url=base_url, model=model,
                prompt=prompt, max_tokens=int(config.get("maxTokens", 4000))))
            if index < total:
                time.sleep(1)
        output = "\n\n".join(part.strip() for part in parts)
        if integrity_check(body, output):
            return output
        last_error = f"第 {attempt}/{attempts} 次尝试的输出未通过完整性校验"
        print(f"[重试] {last_error}", file=sys.stderr)
    raise RuntimeError(f"完整性校验失败：{last_error}，已保留原文件")


def segment_file(txt_path: Path, session, *, api_key: str, base_url: str,
                 config: dict, model: str, dry_run: bool = False,
                 output_dir: Path | None = None,
                 segments: list[tuple[float, float, str]] | None = None) -> str:
    """处理单篇：读文件 → 分段 → 时间标注 →（非 dry-run 时）写回原文件。

    时间标注来源优先用传入的 Whisper 片段；没有时尝试从同名 SRT 恢复。
    返回标注后的完整文本。
    """
    text = txt_path.read_text(encoding="utf-8-sig")
    header_lines, body = split_header(text)
    segmented = segment_body(body, session, api_key=api_key, base_url=base_url,
                             config=config, model=model)
    timings_source = segments if segments else parse_srt_timings(txt_path.with_suffix(".srt"))
    paragraphs = [line for line in segmented.splitlines()
                  if line.strip() and not line.strip().startswith("##")]
    timings = map_paragraph_times(paragraphs, timings_source)
    segmented = annotate_paragraph_times(segmented, timings)
    result = ("\n".join(header_lines) + "\n\n" + segmented) if header_lines else segmented
    if not dry_run:
        txt_path.write_text(result + "\n", encoding="utf-8")
        base_dir = (output_dir or txt_path.parent).resolve()
        try:
            record_path = str(txt_path.relative_to(base_dir)).replace("\\", "/")
        except ValueError:
            record_path = txt_path.name
        state_file = base_dir / DEFAULT_STATE_NAME
        state: dict = {}
        if state_file.is_file():
            try:
                state = json.loads(state_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                state = {}
        state[record_path] = {"model": model, "segmented_at": time.strftime("%Y-%m-%d %H:%M:%S")}
        state_file.write_text(json.dumps(state, ensure_ascii=False, indent=1) + "\n",
                              encoding="utf-8")
    print(f"[分段] {txt_path.name}{'（dry-run 不写回）' if dry_run else ''}", file=sys.stderr)
    return result


# ---------------------------------------------------------------- 批量

def segment_file_auto(
    txt_path: Path,
    *,
    output_dir: Path | None = None,
    config_file: Path | None = None,
    dry_run: bool = False,
    model_override: str | None = None,
    segments: list[tuple[float, float, str]] | None = None,
) -> str:
    """自足的单篇分段入口（供主工具 --segment 和外部程序调用）：自建会话与配置。"""
    config = load_config(config_file or DEFAULT_CONFIG_FILE)
    args = argparse.Namespace(model=model_override)
    api_key, base_url, model = resolve_settings(args)
    if _HAVE_CURL_CFFI:
        session = curl_requests.Session(impersonate="chrome124")
    else:
        session = requests.Session()
    return segment_file(
        txt_path, session, api_key=api_key, base_url=base_url,
        config=config, model=model, dry_run=dry_run,
        output_dir=output_dir, segments=segments)


def iter_pending_files(output_dir: Path, state_file: Path) -> list[Path]:
    """output 下全部 .txt（递归），跳过分段索引和已处理文件。"""
    state: dict = {}
    if state_file.is_file():
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            state = {}
    pending = []
    for txt in sorted(output_dir.rglob("*.txt")):
        if txt.name in STATE_IGNORE:
            continue
        key = str(txt.relative_to(output_dir)).replace("\\", "/")
        if key not in state:
            pending.append(txt)
    return pending


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="segment_transcript",
        description="用中转站 MiniMax 给文字稿做语义分段（含防改写完整性校验）。",
    )
    parser.add_argument("--file", default=None, help="处理单篇文稿；缺省则批量处理 --dir 下未分段文稿")
    parser.add_argument("--dir", default=str(DEFAULT_OUTPUT_DIR), help="批量扫描目录（默认工具目录 output，递归）")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_FILE), help="分段配置文件")
    parser.add_argument("--model", default=None, help="覆盖模型（默认 MINIMAX_SEARCH_MODEL 或 MiniMax-M3）")
    parser.add_argument("--dry-run", action="store_true", help="只看分段结果，不写回文件、不记状态")
    args = parser.parse_args(argv)

    try:
        config = load_config(Path(args.config).resolve())
        api_key, base_url, model = resolve_settings(args)
        if _HAVE_CURL_CFFI:
            session = curl_requests.Session(impersonate="chrome124")
        else:
            session = requests.Session()
        output_dir = Path(args.dir).resolve()

        if args.file:
            files = [Path(args.file).resolve()]
        else:
            files = iter_pending_files(output_dir, output_dir / DEFAULT_STATE_NAME)
            if not files:
                print("[分段] 没有待分段的文稿", file=sys.stderr)
                return 0

        succeeded = 0
        for txt_path in files:
            try:
                segment_file(txt_path, session, api_key=api_key, base_url=base_url,
                             config=config, model=model, dry_run=args.dry_run,
                             output_dir=output_dir)
                succeeded += 1
            except RuntimeError as exc:
                print(f"[失败] {txt_path.name}：{exc}", file=sys.stderr)
            if len(files) > 1:
                time.sleep(1)
        print(f"[完成] 本次分段 {succeeded}/{len(files)} 篇", file=sys.stderr)
    except RuntimeError as exc:
        print(f"[失败] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
