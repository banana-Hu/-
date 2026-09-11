"""segment_transcript.py 的单元测试（假 session，不调真实 MiniMax API）。"""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any


_CANDIDATES = [
    Path(__file__).resolve().parents[1] / "segment_transcript.py",
    Path(__file__).resolve().parents[1] / "tools" / "douyin_transcript" / "segment_transcript.py",
]
MODULE_PATH = next((p for p in _CANDIDATES if p.is_file()), _CANDIDATES[0])
SPEC = importlib.util.spec_from_file_location("segment_transcript", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"无法加载待测试模块：{MODULE_PATH}")
segment_transcript = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(segment_transcript)


CONFIG = {
    "version": 1,
    "model": "MiniMax-M3",
    "maxTokens": 4000,
    "chunkChars": 50,
    "headings": True,
    "prompt": "固定提示词。",
}

BODY = "第一句话。第二句话。第三句话。第四句话。第五句话。第六句话。"


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, replies: list[str] | None = None):
        self.calls: list[dict[str, Any]] = []
        self.replies = replies or []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "json": json})
        reply = self.replies.pop(0) if self.replies else ""
        payload = {
            "content": [{"type": "text", "text": reply}],
            "base_resp": {"status_code": 0, "status_msg": "success"},
        }
        return FakeResponse(200, payload)


class TextProcessingTest(unittest.TestCase):
    def test_split_header_detects_meta_block(self):
        text = "# 标题\n- 作者：A\n- 视频：https://x\n\n正文开始。"
        header, body = segment_transcript.split_header(text)
        self.assertEqual(len(header), 3)
        self.assertEqual(body, "正文开始。")

    def test_split_header_without_meta(self):
        header, body = segment_transcript.split_header("纯正文。")
        self.assertEqual(header, [])
        self.assertEqual(body, "纯正文。")

    def test_split_header_keeps_h2_in_body(self):
        # “## ”是模型输出的小标题，不能被当成 meta 头部剥掉
        header, body = segment_transcript.split_header("## 小标题\n正文。")
        self.assertEqual(header, [])
        self.assertEqual(body, "## 小标题\n正文。")

    def test_split_sentences_keeps_punctuation(self):
        sentences = segment_transcript.split_sentences("甲。乙！丙？丁")
        self.assertEqual(sentences, ["甲。", "乙！", "丙？", "丁"])

    def test_chunk_body_respects_limit_at_sentence_boundary(self):
        chunks = segment_transcript.chunk_body("甲" * 30 + "。" + "乙" * 30 + "。", 35)
        self.assertEqual(chunks, ["甲" * 30 + "。", "乙" * 30 + "。"])
        self.assertTrue(all(len(c) <= 36 for c in chunks))


class IntegrityTest(unittest.TestCase):
    def test_accepts_headings_and_reflow(self):
        output = "## 话题\n第一句话。第二句话。\n\n第三句话。第四句话。\n\n## 另一话题\n第五句话。第六句话。"
        self.assertTrue(segment_transcript.integrity_check(BODY, output))

    def test_rejects_rewritten_text(self):
        output = "第一句话。第二句话被改写了。\n\n第三句话。"
        self.assertFalse(segment_transcript.integrity_check(BODY, output))

    def test_rejects_dropped_content(self):
        self.assertFalse(segment_transcript.integrity_check(BODY, "第一句话。第二句话。"))

    def test_rejects_empty(self):
        self.assertFalse(segment_transcript.integrity_check(BODY, "  \n "))


class SegmentBodyTest(unittest.TestCase):
    def test_multi_chunk_concatenation_and_prompt_position(self):
        # 回复必须逐字保留原文（只加分隔与小标题），否则完整性校验会拦截
        part1, part2 = "甲" * 45 + "。", "乙" * 45 + "。"
        session = FakeSession(replies=[f"## 前半\n{part1}", f"## 后半\n{part2}"])
        body = part1 + part2
        result = segment_transcript.segment_body(
            body, session, api_key="k", base_url="https://relay",
            config=CONFIG, model="m")
        self.assertEqual(len(session.calls), 2)
        self.assertIn("第 1/2 部分", session.calls[0]["json"]["messages"][0]["content"])
        self.assertIn("第 2/2 部分", session.calls[1]["json"]["messages"][0]["content"])
        self.assertEqual(result, f"## 前半\n{part1}\n\n## 后半\n{part2}")

    def test_integrity_failure_raises(self):
        # 3 次重试全部改写原文 → 最终抛出完整性校验失败
        session = FakeSession(replies=["胡乱改写。", "又改写了。", "还是改了。"])
        with self.assertRaisesRegex(RuntimeError, "完整性校验失败"):
            segment_transcript.segment_body(
                BODY, session, api_key="k", base_url="https://relay",
                config=CONFIG, model="m")

    def test_headers_sent_to_api(self):
        session = FakeSession(replies=["第一句话。第二句话。\n\n第三句话。第四句话。\n\n第五句话。第六句话。"])
        segment_transcript.segment_body(
            BODY, session, api_key="kk", base_url="https://relay/",
            config=CONFIG, model="mm")
        headers = session.calls[0]["headers"]
        self.assertEqual(headers["Authorization"], "Bearer kk")
        self.assertEqual(headers["anthropic-version"], "2023-06-01")
        self.assertTrue(session.calls[0]["url"].endswith("/v1/messages"))


class SegmentFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.txt = self.root / "视频.txt"
        self.txt.write_text("# 标题\n- 作者：A\n\n" + BODY, encoding="utf-8")
        self.state = self.root / "segment_state.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _segment(self, reply: str, dry_run=False) -> str:
        session = FakeSession(replies=[reply])
        return segment_transcript.segment_file(
            self.txt, session, api_key="k", base_url="https://relay",
            config=CONFIG, model="m", dry_run=dry_run, output_dir=self.root)

    def test_success_rewrites_file_and_keeps_header(self):
        reply = "## 开头\n第一句话。第二句话。\n\n第三句话。第四句话。\n\n第五句话。第六句话。"
        self._segment(reply)
        content = self.txt.read_text(encoding="utf-8")
        self.assertTrue(content.startswith("# 标题\n- 作者：A\n\n## 开头"))
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertIn("视频.txt", state)

    def test_integrity_failure_keeps_original(self):
        original = self.txt.read_text(encoding="utf-8")
        with self.assertRaises(RuntimeError):
            self._segment("胡乱改写。")
        self.assertEqual(self.txt.read_text(encoding="utf-8"), original)
        self.assertFalse(self.state.exists())

    def test_dry_run_does_not_touch_file_or_state(self):
        original = self.txt.read_text(encoding="utf-8")
        reply = "## 开头\n" + "第一句话。第二句话。\n\n第三句话。第四句话。\n\n第五句话。第六句话。"
        result = self._segment(reply, dry_run=True)
        self.assertEqual(self.txt.read_text(encoding="utf-8"), original)
        self.assertFalse(self.state.exists())
        self.assertIn("## 开头", result)

    def test_pending_scan_skips_state_and_index(self):
        (self.root / "分类").mkdir()
        (self.root / "分类" / "已分段.txt").write_text("x", encoding="utf-8")
        (self.root / "新文稿.txt").write_text("x", encoding="utf-8")
        # 视频.txt（setUp 创建）与已分段文件都登记进状态 → 只剩新文稿待处理
        self.state.write_text(json.dumps({
            "视频.txt": {}, "分类/已分段.txt": {},
        }), encoding="utf-8")
        (self.root / "classifications.jsonl").write_text("", encoding="utf-8")
        pending = segment_transcript.iter_pending_files(self.root, self.state)
        self.assertEqual([p.name for p in pending], ["新文稿.txt"])


if __name__ == "__main__":
    unittest.main()
