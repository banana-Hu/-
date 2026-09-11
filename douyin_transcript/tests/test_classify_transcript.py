"""classify_transcript.py 的单元测试（假 session，不调真实 MiniMax API）。"""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "tools"
    / "douyin_transcript"
    / "classify_transcript.py"
)
SPEC = importlib.util.spec_from_file_location("classify_transcript", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"无法加载待测试模块：{MODULE_PATH}")
classify_transcript = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(classify_transcript)


CONFIG = {
    "version": 1,
    "model": "MiniMax-M3",
    "maxTokens": 50,
    "maxChars": 100,
    "categories": ["AI与技术", "财经商业", "社会时事", "其他"],
    "prompt": "固定提示词：只输出类别名称。",
}


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, payload=None, status_code=200):
        self.calls: list[dict[str, Any]] = []
        self.payload = payload or {
            "content": [{"type": "text", "text": "AI与技术"}],
            "base_resp": {"status_code": 0, "status_msg": "success"},
        }
        self.status_code = status_code

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "json": json})
        return FakeResponse(self.status_code, self.payload, str(self.payload))


class ReplyParsingTest(unittest.TestCase):
    def test_extract_reply_text(self):
        payload = {"content": [
            {"type": "text", "text": "第一段"},
            {"type": "other_block"},
            {"type": "text", "text": "第二段"},
        ]}
        self.assertEqual(classify_transcript.extract_reply_text(payload), "第一段第二段")

    def test_extract_reply_text_missing_content_raises(self):
        with self.assertRaises(RuntimeError):
            classify_transcript.extract_reply_text({"foo": 1})

    def test_match_category_exact_and_fallback(self):
        cats = ["AI与技术", "财经商业", "其他"]
        self.assertEqual(classify_transcript.match_category("AI与技术", cats), "AI与技术")
        # 回复带引号/句号等修饰时仍能匹配
        self.assertEqual(classify_transcript.match_category("「财经商业」。", cats), "财经商业")
        # 完全不匹配时归入“其他”
        self.assertEqual(classify_transcript.match_category("美食类", cats), "其他")


class PromptBuildingTest(unittest.TestCase):
    def test_prompt_truncates_transcript(self):
        text = "甲" * 500
        prompt = classify_transcript.build_prompt(CONFIG["prompt"], text, 100)
        self.assertIn("固定提示词", prompt)
        self.assertIn("甲" * 100, prompt)
        self.assertNotIn("甲" * 101, prompt)


class ClassifyOneTest(unittest.TestCase):
    def test_request_structure_and_headers(self):
        session = FakeSession()
        category = classify_transcript.classify_one(
            "测试文字稿内容", session,
            api_key="fake-key", base_url="https://relay.example",
            config=CONFIG, model="MiniMax-M3")
        self.assertEqual(category, "AI与技术")
        call = session.calls[0]
        self.assertEqual(call["url"], "https://relay.example/v1/messages")
        self.assertEqual(call["headers"]["Authorization"], "Bearer fake-key")
        self.assertEqual(call["headers"]["anthropic-version"], "2023-06-01")
        self.assertEqual(call["json"]["model"], "MiniMax-M3")
        self.assertEqual(call["json"]["max_tokens"], 50)

    def test_http_error_raises(self):
        session = FakeSession(status_code=500)
        with self.assertRaisesRegex(RuntimeError, "500"):
            classify_transcript.classify_one(
                "x", session, api_key="k", base_url="https://relay.example",
                config=CONFIG, model="m")

    def test_business_error_raises(self):
        payload = {"content": [], "base_resp": {"status_code": 1004, "status_msg": "invalid"}}
        session = FakeSession(payload=payload)
        with self.assertRaisesRegex(RuntimeError, "1004"):
            classify_transcript.classify_one(
                "x", session, api_key="k", base_url="https://relay.example",
                config=CONFIG, model="m")


class ArchiveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.output_dir = self.root / "output"
        self.output_dir.mkdir()
        self.txt = self.output_dir / "视频.txt"
        self.txt.write_text("# 标题\n正文", encoding="utf-8")
        (self.output_dir / "视频.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nx\n", encoding="utf-8")
        self.index = self.output_dir / "classifications.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_archive_moves_txt_and_srt(self):
        dest = classify_transcript.archive_transcript(
            self.txt, "AI与技术", self.output_dir, self.index, "MiniMax-M3")
        self.assertTrue(dest.is_file())
        self.assertEqual(dest.parent.name, "AI与技术")
        self.assertFalse(self.txt.exists())
        self.assertFalse((self.output_dir / "视频.srt").exists())
        self.assertTrue((dest.parent / "视频.srt").is_file())
        record = json.loads(self.index.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(record["category"], "AI与技术")
        self.assertEqual(record["file"], "AI与技术/视频.txt")

    def test_archive_dry_run_moves_nothing(self):
        classify_transcript.archive_transcript(
            self.txt, "社会时事", self.output_dir, self.index, "m", dry_run=True)
        self.assertTrue(self.txt.is_file())
        self.assertFalse(self.index.exists())


class PendingScanTest(unittest.TestCase):
    def test_only_root_level_txt_is_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "待分类.txt").write_text("x", encoding="utf-8")
            cat_dir = root / "AI与技术"
            cat_dir.mkdir()
            (cat_dir / "已分类.txt").write_text("x", encoding="utf-8")
            pending = classify_transcript.iter_pending_files(root)
            self.assertEqual([p.name for p in pending], ["待分类.txt"])


if __name__ == "__main__":
    unittest.main()
