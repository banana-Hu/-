"""GPT（codex）编写、ZCode 落盘的批量上传脚本：把 output\\*.txt 原样传为 Notion 子页面。

文字稿内容只经磁盘与 Notion API 传输，不经过任何大模型上下文。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import unquote, urlparse

import requests


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parents[1]
DEFAULT_INPUT_DIR = SCRIPT_DIR / "output"
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env.local"
DEFAULT_STATE_FILE = SCRIPT_DIR / "upload_state.json"

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

UUID_PATTERN = re.compile(
    r"(?<![0-9a-fA-F])"
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
    r"(?![0-9a-fA-F])"
)
HEX_ID_PATTERN = re.compile(
    r"(?<![0-9a-fA-F])([0-9a-fA-F]{32})(?![0-9a-fA-F])"
)
BLANK_LINE_PATTERN = re.compile(r"(?:\r?\n)[ \t]*(?:\r?\n)+")


def load_env_file(path: Path) -> dict[str, str]:
    """读取简单的 KEY=VALUE 环境配置文件。"""
    values: dict[str, str] = {}
    if not path.exists():
        return values

    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise RuntimeError(f"无法读取配置文件：{path}：{exc}") from exc

    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise RuntimeError(
                f"配置文件第 {line_number} 行格式错误，应为 KEY=VALUE：{path}"
            )

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            raise RuntimeError(f"配置文件第 {line_number} 行缺少键名：{path}")

        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {"'", '"'}
        ):
            value = value[1:-1]

        values[key] = value

    return values


def resolve_setting(
    command_line_value: str | None,
    environment: Mapping[str, str],
    file_values: Mapping[str, str],
    name: str,
) -> str | None:
    """按命令行、环境变量、配置文件的顺序解析配置。"""
    if command_line_value is not None and command_line_value.strip():
        return command_line_value.strip()

    environment_value = environment.get(name)
    if environment_value is not None and environment_value.strip():
        return environment_value.strip()

    file_value = file_values.get(name)
    if file_value is not None and file_value.strip():
        return file_value.strip()

    return None


def require_notion_settings(
    notion_token: str | None,
    notion_parent: str | None,
) -> tuple[str, str]:
    """确认 Notion 密钥和父页面配置完整。"""
    missing: list[str] = []
    if not notion_token:
        missing.append("NOTION_TOKEN")
    if not notion_parent:
        missing.append("NOTION_PARENT")

    if missing:
        names = "、".join(missing)
        raise RuntimeError(
            f"缺少配置：{names}。请前往 https://www.notion.so/my-integrations "
            "创建集成，并在目标页面的“…”菜单中通过“连接”添加该集成；"
            "然后设置 NOTION_TOKEN 和 NOTION_PARENT。"
        )

    return notion_token, notion_parent


def parse_notion_parent(value: str) -> str:
    """从页面 URL、连字符 UUID 或裸 32 位十六进制值中提取页面 ID。"""
    candidate = value.strip()
    if not candidate:
        raise ValueError("NOTION_PARENT 不能为空")

    decoded = unquote(candidate)

    # 必须先识别结构完整的 UUID，不能先删除 URL slug 中的连字符。
    uuid_match = UUID_PATTERN.search(decoded)
    if uuid_match:
        return uuid_match.group(1).replace("-", "").lower()

    hex_match = HEX_ID_PATTERN.search(decoded)
    if hex_match:
        return hex_match.group(1).lower()

    # URL 的 slug 可能包含字母和连字符，禁止把整个 slug 去连字符后当作 ID。
    parsed = urlparse(decoded)
    if parsed.scheme or parsed.netloc:
        location = parsed.path or decoded
        raise ValueError(f"无法从 Notion 页面 URL 中解析 32 位页面 ID：{location}")

    raise ValueError(
        "NOTION_PARENT 必须是 Notion 页面 URL、8-4-4-4-12 UUID "
        "或裸 32 位十六进制页面 ID"
    )


def choose_title(file_path: Path, content: str) -> tuple[str, str]:
    """选择页面标题，并返回去除标题行后的正文。"""
    lines = content.splitlines(keepends=True)
    if lines:
        first_line = lines[0].rstrip("\r\n")
        if first_line.startswith("# "):
            title = first_line[2:].strip()
            if title:
                return title, "".join(lines[1:])

    return file_path.stem, content


def split_rich_text(text: str, limit: int = 2000) -> list[str]:
    """将文本按 Notion 单个 rich_text 的字符上限切分。"""
    if limit <= 0:
        raise ValueError("切分长度必须大于零")
    if not text:
        return []
    return [text[index:index + limit] for index in range(0, len(text), limit)]


def split_body_paragraphs(body: str) -> list[str]:
    """按空行切分正文，保留每个非空段落内部的原始文本。"""
    if not body:
        return []

    paragraphs = BLANK_LINE_PATTERN.split(body)
    return [paragraph for paragraph in paragraphs if paragraph != ""]


def make_text_block(text: str) -> dict[str, Any]:
    """创建普通段落块。"""
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {
            "rich_text": [
                {
                    "type": "text",
                    "text": {"content": text},
                }
            ]
        },
    }


def make_divider_block() -> dict[str, Any]:
    """创建分隔线块。"""
    return {
        "object": "block",
        "type": "divider",
        "divider": {},
    }


def build_children(file_name: str, body: str) -> list[dict[str, Any]]:
    """构建来源文件、分隔线和正文块。"""
    children: list[dict[str, Any]] = [
        make_text_block(f"来源文件：{file_name}"),
        make_divider_block(),
    ]

    for paragraph in split_body_paragraphs(body):
        for piece in split_rich_text(paragraph):
            children.append(make_text_block(piece))

    return children


def load_state(path: Path) -> dict[str, dict[str, str]]:
    """读取上传状态；不存在时返回空状态。"""
    if not path.exists():
        return {}

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法读取上传状态文件：{path}：{exc}") from exc

    if not isinstance(raw, dict):
        raise RuntimeError(f"上传状态文件格式错误，顶层必须是对象：{path}")

    state: dict[str, dict[str, str]] = {}
    for file_name, record in raw.items():
        if isinstance(file_name, str) and isinstance(record, dict):
            state[file_name] = {
                str(key): str(value)
                for key, value in record.items()
                if isinstance(key, str)
            }
    return state


def save_state(path: Path, state: Mapping[str, Mapping[str, str]]) -> None:
    """以替换文件的方式保存上传状态。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f"{path.name}.tmp")

    try:
        temporary_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary_path.replace(path)
    except OSError as exc:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise RuntimeError(f"无法保存上传状态文件：{path}：{exc}") from exc


class NotionClient:
    """封装可注入 HTTP 会话的 Notion API 客户端。"""

    def __init__(
        self,
        token: str,
        session: Any | None = None,
        api_base: str = NOTION_API_BASE,
        timeout: float = 30.0,
    ) -> None:
        self.session = session if session is not None else requests.Session()
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }

    def _request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        url = f"{self.api_base}{path}"

        try:
            requester = getattr(self.session, method.lower())
            response = requester(
                url,
                headers=self.headers,
                json=payload,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"请求 Notion API 失败：{exc}") from exc
        except (AttributeError, TypeError) as exc:
            raise RuntimeError(f"HTTP 会话调用失败：{exc}") from exc

        status_code = getattr(response, "status_code", None)
        if not isinstance(status_code, int):
            raise RuntimeError("Notion API 响应缺少有效状态码")

        if not 200 <= status_code < 300:
            response_text = getattr(response, "text", "")
            detail = str(response_text).strip()
            if len(detail) > 500:
                detail = detail[:500] + "…"
            suffix = f"：{detail}" if detail else ""
            raise RuntimeError(
                f"Notion API 请求失败，状态码 {status_code}{suffix}"
            )

        try:
            data = response.json()
        except (ValueError, TypeError) as exc:
            raise RuntimeError(
                f"Notion API 返回了无法解析的 JSON，状态码 {status_code}"
            ) from exc

        if not isinstance(data, dict):
            raise RuntimeError(
                f"Notion API 返回格式错误，状态码 {status_code}"
            )
        return data

    def create_page(
        self,
        parent_page_id: str,
        title: str,
        children: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """创建页面，并在需要时分批追加剩余内容块。"""
        first_batch = list(children[:100])
        payload = {
            "parent": {"page_id": parent_page_id},
            "properties": {
                "title": {
                    "type": "title",
                    "title": [
                        {
                            "type": "text",
                            "text": {"content": title},
                        }
                    ],
                }
            },
            "children": first_batch,
        }
        page = self._request("post", "/pages", payload)

        page_id = page.get("id")
        if not isinstance(page_id, str) or not page_id:
            raise RuntimeError("Notion 创建页面成功，但响应中缺少页面 ID")

        for start in range(100, len(children), 100):
            batch = list(children[start:start + 100])
            self._request(
                "patch",
                f"/blocks/{page_id}/children",
                {"children": batch},
            )

        return page


def upload_directory(
    input_dir: Path,
    client: NotionClient,
    parent_page_id: str,
    state_path: Path = DEFAULT_STATE_FILE,
    *,
    force: bool = False,
    dry_run: bool = False,
    output: Any = None,
) -> dict[str, int]:
    """批量上传目录下的文本文件。"""
    stream = output if output is not None else sys.stdout

    if not input_dir.exists():
        raise RuntimeError(f"扫描目录不存在：{input_dir}")
    if not input_dir.is_dir():
        raise RuntimeError(f"扫描路径不是目录：{input_dir}")

    files = sorted(
        (
            path
            for path in input_dir.iterdir()
            if path.is_file() and path.suffix.lower() == ".txt"
        ),
        key=lambda path: path.name.casefold(),
    )

    state = load_state(state_path)
    result = {"uploaded": 0, "skipped": 0, "planned": 0}

    for file_path in files:
        if file_path.name in state and not force:
            print(f"跳过已上传文件：{file_path.name}", file=stream)
            result["skipped"] += 1
            continue

        try:
            content = file_path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as exc:
            raise RuntimeError(f"无法读取文本文件：{file_path}：{exc}") from exc

        title, body = choose_title(file_path, content)
        children = build_children(file_path.name, body)

        if dry_run:
            print(
                f"试运行：{file_path.name} | 标题：{title} | "
                f"预计块数：{len(children)}",
                file=stream,
            )
            result["planned"] += 1
            continue

        page = client.create_page(parent_page_id, title, children)
        page_url = page.get("url")
        if not isinstance(page_url, str):
            page_url = ""

        state[file_path.name] = {
            "url": page_url,
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
        }
        save_state(state_path, state)

        print(f"上传完成：{file_path.name} -> {page_url}", file=stream)
        result["uploaded"] += 1

    return result


def build_argument_parser() -> argparse.ArgumentParser:
    """创建命令行参数解析器。"""
    parser = argparse.ArgumentParser(
        description="把抖音转写目录中的 .txt 文件批量上传为 Notion 子页面。"
    )
    parser.add_argument(
        "--notion-token",
        help="Notion 集成密钥，优先于环境变量和 .env.local。",
    )
    parser.add_argument(
        "--notion-parent",
        help="Notion 父页面 URL 或页面 ID，优先于环境变量和 .env.local。",
    )
    parser.add_argument(
        "--dir",
        dest="input_dir",
        type=Path,
        default=DEFAULT_INPUT_DIR,
        help=f"扫描目录，默认：{DEFAULT_INPUT_DIR}",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="忽略 upload_state.json，对已记录文件重新上传。",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印标题和预计块数，不发送请求或修改状态。",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """运行命令行上传程序。"""
    args = build_argument_parser().parse_args(argv)
    file_values = load_env_file(DEFAULT_ENV_FILE)

    notion_token = resolve_setting(
        args.notion_token,
        os.environ,
        file_values,
        "NOTION_TOKEN",
    )
    notion_parent = resolve_setting(
        args.notion_parent,
        os.environ,
        file_values,
        "NOTION_PARENT",
    )

    if args.dry_run:
        # 试运行不调 API，允许在未配置密钥时预览上传计划
        parent_page_id = "0" * 32
        client = None
    else:
        notion_token, notion_parent = require_notion_settings(
            notion_token,
            notion_parent,
        )
        try:
            parent_page_id = parse_notion_parent(notion_parent)
        except ValueError as exc:
            raise RuntimeError(f"NOTION_PARENT 无效：{exc}") from exc

        client = NotionClient(notion_token)

    upload_directory(
        args.input_dir.resolve(),
        client,
        parent_page_id,
        DEFAULT_STATE_FILE,
        force=args.force,
        dry_run=args.dry_run,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1)
