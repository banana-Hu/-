"""classify_transcript —— 抖音文字稿内容领域自动分类器。

读取 output\\ 下的文字稿，用固定提示词调用中转站 MiniMax 判定内容领域，
然后把文稿（及同名 SRT）归档到 output\\<类别>\\ 子目录，并记录到
classifications.jsonl。配置在 classify_config.json（固定提示词 + 类别清单）。

密钥复用项目既有约定：.env.local 的 MINIMAX_SEARCH_API_KEY /
MINIMAX_SEARCH_API_BASE_URL，模型可用 MINIMAX_SEARCH_MODEL 覆盖。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

import requests

try:
    # 与主工具同目录；打包时 PyInstaller 会一并分析
    from curl_cffi import requests as curl_requests  # noqa: F401
    _HAVE_CURL_CFFI = True
except ImportError:  # pragma: no cover
    _HAVE_CURL_CFFI = False

TOOL_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_FILE = TOOL_DIR / "classify_config.json"
DEFAULT_OUTPUT_DIR = TOOL_DIR / "output"
DEFAULT_INDEX_NAME = "classifications.jsonl"

ENV_FILE = TOOL_DIR.parents[1] / ".env.local"
ENV_KEY = "MINIMAX_SEARCH_API_KEY"
ENV_BASE_URL = "MINIMAX_SEARCH_API_BASE_URL"
ENV_MODEL = "MINIMAX_SEARCH_MODEL"
DEFAULT_BASE_URL = "https://api.minimaxi.com/anthropic"
DEFAULT_MODEL = "MiniMax-M3"
MESSAGES_PATH = "/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
FALLBACK_CATEGORY = "其他"


# ---------------------------------------------------------------- 配置加载

def load_local_env() -> None:
    """读取未提交的 .env.local（不覆盖已有环境变量），并清除系统代理。

    中转站为国内可达地址，直连即可；本机残留的失效代理会把请求导向死端口。
    """
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
    """加载分类配置（固定提示词 + 类别清单）。"""
    config = json.loads(config_file.read_text(encoding="utf-8"))
    for key in ("categories", "prompt"):
        if not config.get(key):
            raise RuntimeError(f"分类配置缺少 {key}：{config_file}")
    return config


def resolve_settings(args: argparse.Namespace) -> tuple[str, str, str]:
    """解析 api_key / base_url / model，优先级：环境变量 > .env.local > 默认值。"""
    load_local_env()
    api_key = os.environ.get(ENV_KEY, "").strip()
    if not api_key:
        raise RuntimeError(
            f"缺少 {ENV_KEY}。请在 {ENV_FILE} 中配置（与搜索发现共用同一密钥）。"
        )
    base_url = os.environ.get(ENV_BASE_URL, "").strip() or DEFAULT_BASE_URL
    model = os.environ.get(ENV_MODEL, "").strip() or args.model or DEFAULT_MODEL
    return api_key, base_url.rstrip("/"), model


# ---------------------------------------------------------------- MiniMax 调用

def build_prompt(prompt: str, transcript: str, max_chars: int) -> str:
    """固定提示词 + 截断后的文字稿（只取前 max_chars 字，控制 token 消耗）。"""
    return f"{prompt}\n\n文字稿：\n{transcript[:max_chars]}"


def extract_reply_text(payload: dict) -> str:
    """从 Anthropic 风格响应中取出全部文本块。"""
    blocks = payload.get("content")
    if not isinstance(blocks, list):
        raise RuntimeError(f"MiniMax 响应缺少 content 数组：{str(payload)[:200]}")
    parts = []
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "".join(parts).strip()


def match_category(reply: str, categories: list[str]) -> str:
    """从模型回复中匹配类别；匹配不到归入“其他”。"""
    reply = reply.strip().strip('"「」『』。.，, ')
    for category in categories:
        if category in reply:
            return category
    return FALLBACK_CATEGORY if FALLBACK_CATEGORY in categories else categories[-1]


def classify_one(
    transcript: str,
    session,
    *,
    api_key: str,
    base_url: str,
    config: dict,
    model: str,
    timeout: int = 60,
) -> str:
    """调用中转站 MiniMax，返回该文稿的内容领域类别。"""
    body = {
        "model": model,
        "max_tokens": int(config.get("maxTokens", 50)),
        "messages": [
            {"role": "user", "content": build_prompt(
                config["prompt"], transcript, int(config.get("maxChars", 2500)))}
        ],
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "anthropic-version": ANTHROPIC_VERSION,
    }
    try:
        resp = session.post(
            f"{base_url}{MESSAGES_PATH}", headers=headers, json=body, timeout=timeout)
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
            f"MiniMax 业务错误 {base_resp.get('status_code')}：{base_resp.get('status_msg')}"
        )
    reply = extract_reply_text(payload)
    if not reply:
        raise RuntimeError("MiniMax 返回了空回复")
    return match_category(reply, config["categories"])


# ---------------------------------------------------------------- 归档

def archive_transcript(
    txt_path: Path, category: str, output_dir: Path, index_file: Path,
    model: str, dry_run: bool = False,
) -> Path:
    """把文稿（及同名 SRT）移入 output/<类别>/，并追加分类索引记录。"""
    target_dir = output_dir / category
    moved: list[Path] = []
    if not dry_run:
        target_dir.mkdir(parents=True, exist_ok=True)
        for source in (txt_path, txt_path.with_suffix(".srt")):
            if source.is_file():
                dest = target_dir / source.name
                shutil.move(str(source), str(dest))
                moved.append(dest)
    record = {
        "file": str((target_dir / txt_path.name).relative_to(output_dir)).replace("\\", "/"),
        "category": category,
        "model": model,
        "classified_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if not dry_run:
        with open(index_file, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(f"[分类] {txt_path.name} -> {category}", file=sys.stderr)
    return target_dir / txt_path.name


# ---------------------------------------------------------------- 主流程

def iter_pending_files(output_dir: Path) -> list[Path]:
    """待分类文稿 = output 根目录下的 .txt（类别子目录里的视为已归档）。"""
    return sorted(
        p for p in output_dir.glob("*.txt") if p.is_file()
    )


def classify_all(
    output_dir: Path,
    config_file: Path,
    *,
    dry_run: bool = False,
    model_override: str | None = None,
) -> int:
    """批量分类 output 根目录下的所有文稿，返回成功条数。"""
    config = load_config(config_file)
    api_key, base_url, model = resolve_settings(args_namespace(model_override))
    if _HAVE_CURL_CFFI:
        session = curl_requests.Session(impersonate="chrome124")
    else:
        session = requests.Session()

    pending = iter_pending_files(output_dir)
    if not pending:
        print("[分类] 没有待分类的文稿", file=sys.stderr)
        return 0
    index_file = output_dir / DEFAULT_INDEX_NAME
    succeeded = 0
    for txt_path in pending:
        try:
            transcript = txt_path.read_text(encoding="utf-8-sig")
            category = classify_one(
                transcript, session,
                api_key=api_key, base_url=base_url, config=config, model=model)
            archive_transcript(
                txt_path, category, output_dir, index_file, model,
                dry_run=dry_run)
            succeeded += 1
        except RuntimeError as exc:
            print(f"[失败] {txt_path.name}：{exc}", file=sys.stderr)
        time.sleep(1)  # 轻微限速，避免触发中转站限流
    print(f"[完成] 本次分类 {succeeded}/{len(pending)} 篇", file=sys.stderr)
    return succeeded


def classify_file(
    txt_path: Path,
    *,
    output_dir: Path | None = None,
    config_file: Path | None = None,
    dry_run: bool = False,
    model_override: str | None = None,
) -> str:
    """对单个文稿做分类并归档，供主工具 --classify 和外部程序调用。返回类别。"""
    output_dir = (output_dir or DEFAULT_OUTPUT_DIR).resolve()
    config = load_config(config_file or DEFAULT_CONFIG_FILE)
    api_key, base_url, model = resolve_settings(args_namespace(model_override))
    if _HAVE_CURL_CFFI:
        session = curl_requests.Session(impersonate="chrome124")
    else:
        session = requests.Session()
    transcript = txt_path.read_text(encoding="utf-8-sig")
    category = classify_one(
        transcript, session,
        api_key=api_key, base_url=base_url, config=config, model=model)
    archive_transcript(
        txt_path, category, output_dir, output_dir / DEFAULT_INDEX_NAME, model,
        dry_run=dry_run)
    return category


def args_namespace(model_override: str | None) -> argparse.Namespace:
    """resolve_settings 需要的最小命名空间。"""
    return argparse.Namespace(model=model_override)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="classify_transcript",
        description="用固定提示词调用中转站 MiniMax，把 output 下的文字稿按内容领域自动归档。",
    )
    parser.add_argument("--dir", default=str(DEFAULT_OUTPUT_DIR), help="文稿目录（默认工具目录 output）")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_FILE), help="分类配置文件")
    parser.add_argument("--model", default=None, help="覆盖模型（默认读 MINIMAX_SEARCH_MODEL 或 MiniMax-M3）")
    parser.add_argument("--dry-run", action="store_true", help="只输出分类结果，不移动文件、不写索引")
    args = parser.parse_args(argv)

    try:
        classify_all(
            Path(args.dir).resolve(),
            Path(args.config).resolve(),
            dry_run=args.dry_run,
            model_override=args.model,
        )
    except RuntimeError as exc:
        print(f"[失败] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
