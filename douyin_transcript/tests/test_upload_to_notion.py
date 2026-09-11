"""upload_to_notion.py 的单元测试（GPT 起草，路径改为仓库相对定位）。"""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any


MODULE_PATH = Path(__file__).resolve().parents[1] / "upload_to_notion.py"

SPEC = importlib.util.spec_from_file_location("upload_to_notion", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"无法加载待测试模块：{MODULE_PATH}")

upload_to_notion = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(upload_to_notion)


class FakeResponse:
    """供测试使用的 HTTP 响应。"""

    def __init__(
        self,
        status_code: int = 200,
        data: dict[str, Any] | None = None,
        text: str = "",
    ) -> None:
        self.status_code = status_code
        self._data = data if data is not None else {}
        self.text = text

    def json(self) -> dict[str, Any]:
        return self._data


class FakeSession:
    """记录请求并返回预设响应的假 HTTP 会话。"""

    def __init__(
        self,
        responses: list[FakeResponse] | None = None,
    ) -> None:
        self.responses = list(responses or [])
        self.calls: list[dict[str, Any]] = []
        self.page_number = 0

    def _respond(
        self,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> FakeResponse:
        self.calls.append(
            {
                "method": method,
                "url": url,
                **kwargs,
            }
        )

        if self.responses:
            return self.responses.pop(0)

        if method == "POST":
            self.page_number += 1
            return FakeResponse(
                200,
                {
                    "id": f"page-{self.page_number}",
                    "url": f"https://notion.so/page-{self.page_number}",
                },
            )

        return FakeResponse(200, {"object": "list", "results": []})

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._respond("POST", url, **kwargs)

    def patch(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._respond("PATCH", url, **kwargs)


class ParseNotionParentTests(unittest.TestCase):
    """测试父页面 ID 解析。"""

    def test_parse_url_slug_with_uuid(self) -> None:
        value = (
            "https://www.notion.so/workspace/"
            "这是页面标题-12345678-1234-1234-1234-123456789abc?pvs=4"
        )
        self.assertEqual(
            upload_to_notion.parse_notion_parent(value),
            "12345678123412341234123456789abc",
        )

    def test_parse_url_slug_with_bare_hex(self) -> None:
        value = (
            "https://www.notion.so/workspace/"
            "weekly-notes-12345678123412341234123456789abc"
        )
        self.assertEqual(
            upload_to_notion.parse_notion_parent(value),
            "12345678123412341234123456789abc",
        )

    def test_parse_hyphenated_uuid(self) -> None:
        self.assertEqual(
            upload_to_notion.parse_notion_parent(
                "12345678-1234-1234-1234-123456789ABC"
            ),
            "12345678123412341234123456789abc",
        )

    def test_parse_bare_hex(self) -> None:
        self.assertEqual(
            upload_to_notion.parse_notion_parent(
                "ABCDEF0123456789ABCDEF0123456789"
            ),
            "abcdef0123456789abcdef0123456789",
        )

    def test_reject_slug_without_page_id(self) -> None:
        with self.assertRaises(ValueError):
            upload_to_notion.parse_notion_parent(
                "https://www.notion.so/workspace/my-lettered-page-slug"
            )

    def test_reject_invalid_value(self) -> None:
        with self.assertRaises(ValueError):
            upload_to_notion.parse_notion_parent("不是页面 ID")


class ContentTests(unittest.TestCase):
    """测试标题选择和内容块生成。"""

    def test_title_uses_first_markdown_heading(self) -> None:
        path = Path("示例.txt")
        title, body = upload_to_notion.choose_title(
            path,
            "# 页面标题\n第一段\n\n第二段",
        )
        self.assertEqual(title, "页面标题")
        self.assertEqual(body, "第一段\n\n第二段")

    def test_title_falls_back_to_file_stem(self) -> None:
        path = Path("文件标题.txt")
        content = "没有标题行\n正文"
        title, body = upload_to_notion.choose_title(path, content)
        self.assertEqual(title, "文件标题")
        self.assertEqual(body, content)

    def test_rich_text_is_split_at_2000_characters(self) -> None:
        pieces = upload_to_notion.split_rich_text("甲" * 4501)
        self.assertEqual([len(piece) for piece in pieces], [2000, 2000, 501])
        self.assertEqual("".join(pieces), "甲" * 4501)

    def test_build_children_keeps_paragraph_text(self) -> None:
        children = upload_to_notion.build_children(
            "示例.txt",
            "第一行\n第二行\n\n第三段",
        )
        self.assertEqual(children[0]["type"], "paragraph")
        self.assertEqual(children[1]["type"], "divider")
        self.assertEqual(
            children[2]["paragraph"]["rich_text"][0]["text"]["content"],
            "第一行\n第二行",
        )
        self.assertEqual(
            children[3]["paragraph"]["rich_text"][0]["text"]["content"],
            "第三段",
        )


class NotionClientTests(unittest.TestCase):
    """测试 Notion 请求和分批规则。"""

    def test_children_are_sent_in_batches_of_at_most_100(self) -> None:
        session = FakeSession()
        client = upload_to_notion.NotionClient(
            "test-token",
            session=session,
        )
        children = [
            upload_to_notion.make_text_block(f"块 {index}")
            for index in range(205)
        ]

        client.create_page(
            "12345678123412341234123456789abc",
            "测试页面",
            children,
        )

        self.assertEqual(len(session.calls), 3)
        self.assertEqual(session.calls[0]["method"], "POST")
        self.assertEqual(len(session.calls[0]["json"]["children"]), 100)
        self.assertEqual(session.calls[1]["method"], "PATCH")
        self.assertEqual(len(session.calls[1]["json"]["children"]), 100)
        self.assertEqual(session.calls[2]["method"], "PATCH")
        self.assertEqual(len(session.calls[2]["json"]["children"]), 5)
        self.assertTrue(
            session.calls[1]["url"].endswith(
                "/v1/blocks/page-1/children"
            )
        )

    def test_request_headers_are_present(self) -> None:
        session = FakeSession()
        client = upload_to_notion.NotionClient(
            "test-token",
            session=session,
        )

        client.create_page(
            "12345678123412341234123456789abc",
            "测试页面",
            [],
        )

        headers = session.calls[0]["headers"]
        self.assertEqual(headers["Authorization"], "Bearer test-token")
        self.assertEqual(headers["Notion-Version"], "2022-06-28")

    def test_non_2xx_raises_runtime_error_with_status_code(self) -> None:
        session = FakeSession(
            [
                FakeResponse(
                    400,
                    {"object": "error"},
                    "请求参数错误",
                )
            ]
        )
        client = upload_to_notion.NotionClient(
            "test-token",
            session=session,
        )

        with self.assertRaisesRegex(RuntimeError, "状态码 400"):
            client.create_page(
                "12345678123412341234123456789abc",
                "测试页面",
                [],
            )


class UploadStateTests(unittest.TestCase):
    """测试上传状态、跳过和强制重传。"""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.input_dir = self.root / "output"
        self.input_dir.mkdir()
        self.state_path = self.root / "upload_state.json"
        self.file_path = self.input_dir / "示例.txt"
        self.file_path.write_text(
            "# 示例标题\n正文内容",
            encoding="utf-8",
        )
        self.parent_id = "12345678123412341234123456789abc"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def make_client(self) -> tuple[Any, FakeSession]:
        session = FakeSession()
        client = upload_to_notion.NotionClient(
            "test-token",
            session=session,
        )
        return client, session

    def test_existing_state_skips_file(self) -> None:
        self.state_path.write_text(
            json.dumps(
                {
                    "示例.txt": {
                        "url": "https://notion.so/existing",
                        "uploaded_at": "2026-09-12T00:00:00+00:00",
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        client, session = self.make_client()

        result = upload_to_notion.upload_directory(
            self.input_dir,
            client,
            self.parent_id,
            self.state_path,
        )

        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["uploaded"], 0)
        self.assertEqual(session.calls, [])

    def test_force_reuploads_existing_file(self) -> None:
        self.state_path.write_text(
            json.dumps(
                {
                    "示例.txt": {
                        "url": "https://notion.so/existing",
                        "uploaded_at": "2026-09-12T00:00:00+00:00",
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        client, session = self.make_client()

        result = upload_to_notion.upload_directory(
            self.input_dir,
            client,
            self.parent_id,
            self.state_path,
            force=True,
        )

        self.assertEqual(result["uploaded"], 1)
        self.assertEqual(result["skipped"], 0)
        self.assertEqual(len(session.calls), 1)

        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(
            state["示例.txt"]["url"],
            "https://notion.so/page-1",
        )
        self.assertIn("uploaded_at", state["示例.txt"])

    def test_dry_run_does_not_call_api_or_write_state(self) -> None:
        client, session = self.make_client()

        result = upload_to_notion.upload_directory(
            self.input_dir,
            client,
            self.parent_id,
            self.state_path,
            dry_run=True,
        )

        self.assertEqual(result["planned"], 1)
        self.assertEqual(session.calls, [])
        self.assertFalse(self.state_path.exists())


class ConfigurationTests(unittest.TestCase):
    """测试配置优先级和缺失提示。"""

    def test_command_line_overrides_environment_and_file(self) -> None:
        value = upload_to_notion.resolve_setting(
            "命令行",
            {"NOTION_TOKEN": "环境变量"},
            {"NOTION_TOKEN": "配置文件"},
            "NOTION_TOKEN",
        )
        self.assertEqual(value, "命令行")

    def test_environment_overrides_file(self) -> None:
        value = upload_to_notion.resolve_setting(
            None,
            {"NOTION_TOKEN": "环境变量"},
            {"NOTION_TOKEN": "配置文件"},
            "NOTION_TOKEN",
        )
        self.assertEqual(value, "环境变量")

    def test_missing_settings_raise_actionable_error(self) -> None:
        with self.assertRaisesRegex(
            RuntimeError,
            r"notion\.so/my-integrations",
        ):
            upload_to_notion.require_notion_settings(None, None)

    def test_env_file_ignores_comments(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            env_path = Path(temporary_directory) / ".env.local"
            env_path.write_text(
                "# 这是注释\n"
                "NOTION_TOKEN=test-token\n"
                "\n"
                "NOTION_PARENT='parent-value'\n",
                encoding="utf-8",
            )

            values = upload_to_notion.load_env_file(env_path)

        self.assertEqual(values["NOTION_TOKEN"], "test-token")
        self.assertEqual(values["NOTION_PARENT"], "parent-value")


if __name__ == "__main__":
    unittest.main()
