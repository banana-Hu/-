"""douyin_transcript 纯函数部分的单元测试（不涉及网络与模型）。"""

import importlib.util
import json
import sys
import unittest
from pathlib import Path

TOOL_PATH = Path(__file__).resolve().parents[1] / "tools" / "douyin_transcript" / "douyin_transcript.py"
spec = importlib.util.spec_from_file_location("douyin_transcript", TOOL_PATH)
douyin_transcript = importlib.util.module_from_spec(spec)
sys.modules["douyin_transcript"] = douyin_transcript
spec.loader.exec_module(douyin_transcript)


class UrlParsingTest(unittest.TestCase):
    def test_direct_video_url(self):
        session = object()  # 直接 URL 不应触发网络请求
        vid = douyin_transcript.extract_video_id(
            "https://www.douyin.com/video/7641191338431597839?foo=bar", session)
        self.assertEqual(vid, "7641191338431597839")

    def test_note_url(self):
        vid = douyin_transcript.extract_video_id(
            "https://www.iesdouyin.com/share/note/7300000000000000000/", object())
        self.assertEqual(vid, "7300000000000000000")

    def test_share_text_blob(self):
        text = "复制打开抖音，看看【xxx的作品】标题 https://v.douyin.com/abcDEF/ 更多内容"

        # 模拟无网络：短链需要 session.get 跳转，失败时应向上抛错
        class BoomSession:
            def get(self, *args, **kwargs):
                raise RuntimeError("network down")

        with self.assertRaises(RuntimeError):
            douyin_transcript.extract_video_id(text, BoomSession())

    def test_no_url_raises_valueerror(self):
        with self.assertRaises(ValueError):
            douyin_transcript.extract_video_id("没有任何链接的文本", object())


class VttParsingTest(unittest.TestCase):
    def test_parse_webvtt(self):
        vtt = (
            "WEBVTT\n\n"
            "00:00:01.000 --> 00:00:03.500\n"
            "大家好，今天讲一下\n\n"
            "00:00:04.000 --> 00:00:06.000\n"
            "<c>第二个片段</c>\n"
        )
        segments = douyin_transcript.parse_vtt(vtt)
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0], (1.0, 3.5, "大家好，今天讲一下"))
        self.assertEqual(segments[1][2], "第二个片段")

    def test_parse_srt_style(self):
        srt = (
            "1\n"
            "00:00:01,000 --> 00:00:02,000\n"
            "第一句\n\n"
            "2\n"
            "00:00:03,000 --> 00:00:04,000\n"
            "第二句\n"
        )
        segments = douyin_transcript.parse_vtt(srt)
        self.assertEqual([s[2] for s in segments], ["第一句", "第二句"])
        self.assertEqual(segments[1][0], 3.0)


class OutputFormattingTest(unittest.TestCase):
    def test_merge_paragraphs_by_gap(self):
        segments = [
            (0.0, 2.0, "连起来的一句"),
            (2.5, 4.0, "话和另一句"),
            (10.0, 12.0, "间隔大应分段"),
        ]
        merged = douyin_transcript.merge_paragraphs(segments)
        self.assertEqual(merged, "连起来的一句话和另一句\n\n间隔大应分段")

    def test_format_srt_index_and_time(self):
        srt = douyin_transcript.format_srt([(1.0, 2.5, "你好")])
        self.assertIn("1\n00:00:01,000 --> 00:00:02,500\n你好", srt)


class CaptionFindingTest(unittest.TestCase):
    def test_find_caption_infos_nested(self):
        detail = {
            "video": {
                "play_addr": {},
                "cla_info": {
                    "caption_infos": [
                        {"lang": "zh", "format": "webvtt",
                         "url_list": ["https://example.com/cap.vtt"]},
                    ],
                },
            },
        }
        caps = douyin_transcript.find_captions(detail)
        self.assertEqual(caps[0]["url_list"][0], "https://example.com/cap.vtt")

    def test_find_caption_infos_absent(self):
        self.assertEqual(douyin_transcript.find_captions({"video": {"play_addr": {}}}), [])


class NotionHelperTest(unittest.TestCase):
    def test_parse_page_id_from_url(self):
        # URL slug 里含字母，不能被并进 ID
        url = "https://www.notion.so/My-Page-1a2b3c4d5e6f7890abcdef1234567890?pvs=4"
        self.assertEqual(douyin_transcript.parse_notion_page_id(url),
                         "1a2b3c4d5e6f7890abcdef1234567890")

    def test_parse_page_id_dashed(self):
        self.assertEqual(douyin_transcript.parse_notion_page_id(
            "1a2b3c4d-5e6f-7890-abcd-ef1234567890"), "1a2b3c4d5e6f7890abcdef1234567890")

    def test_parse_page_id_invalid(self):
        with self.assertRaises(ValueError):
            douyin_transcript.parse_notion_page_id("https://www.notion.so/无ID页面")

    def test_text_blocks_chunking(self):
        # 1 个短段落 + ceil(4500/2000)=3 块
        blocks = douyin_transcript.notion_text_blocks(["短段落", "长" * 4500])
        self.assertEqual(len(blocks), 4)
        self.assertEqual(blocks[1]["paragraph"]["rich_text"][0]["text"]["content"], "长" * 2000)
        self.assertEqual(blocks[-1]["paragraph"]["rich_text"][0]["text"]["content"], "长" * 500)

    def test_text_blocks_skip_empty(self):
        self.assertEqual(douyin_transcript.notion_text_blocks(["", "   "]), [])


class NotionPushTest(unittest.TestCase):
    """用假 session 验证请求报文结构，不依赖真实凭据。"""

    class FakeResponse:
        def __init__(self, status_code=200, payload=None):
            self.status_code = status_code
            self._payload = payload or {}
            self.text = ""

        def json(self):
            return self._payload

    class FakeSession:
        def __init__(self, pages_ok=True):
            self.calls = []
            self.pages_ok = pages_ok

        def post(self, url, headers=None, json=None, timeout=None):
            self.calls.append(("POST", url, json))
            if not self.pages_ok:
                return NotionPushTest.FakeResponse(404, {})
            return NotionPushTest.FakeResponse(200, {"id": "page-1", "url": "https://notion.so/page-1"})

        def patch(self, url, headers=None, json=None, timeout=None):
            self.calls.append(("PATCH", url, json))
            return NotionPushTest.FakeResponse(200, {})

    def _args(self, **kw):
        import argparse
        ns = argparse.Namespace(notion_token="ntn_fake", notion_parent="", **kw)
        ns.notion_parent = kw.get("notion_parent", "1a2b3c4d5e6f7890abcdef1234567890")
        return ns

    def test_creates_page_with_title_and_parent(self):
        session = self.FakeSession()
        url = douyin_transcript.push_to_notion(
            session, self._args(), title="测试标题", author="作者A",
            video_id="123", paragraphs=["第一段", "第二段"])
        self.assertEqual(url, "https://notion.so/page-1")
        method, endpoint, body = session.calls[0]
        self.assertEqual(method, "POST")
        self.assertTrue(endpoint.endswith("/pages"))
        self.assertEqual(body["parent"], {"page_id": "1a2b3c4d5e6f7890abcdef1234567890"})
        self.assertEqual(body["properties"]["title"]["title"][0]["text"]["content"], "测试标题")
        # 正文包含作者、来源链接，以及两个段落
        texts = [b["paragraph"]["rich_text"][0]["text"]["content"]
                 for b in body["children"] if b["type"] == "paragraph"]
        self.assertIn("第一段", texts)
        self.assertIn("第二段", texts)
        self.assertTrue(any("douyin.com/video/123" in t for t in texts))

    def test_batches_children_beyond_100(self):
        session = self.FakeSession()
        douyin_transcript.push_to_notion(
            session, self._args(), title="长文", author="", video_id="1",
            paragraphs=["段落%d" % i for i in range(150)])
        first_post = session.calls[0][2]["children"]
        self.assertEqual(len(first_post), 100)  # 首次请求最多 100 块
        patches = [c for c in session.calls if c[0] == "PATCH"]
        self.assertEqual(len(patches), 1)      # 余下部分走追加接口
        # 150 段 + 2 行元信息（无作者）+ 1 条分隔线 = 153 块
        self.assertEqual(len(patches[0][2]["children"]), 53)

    def test_raises_on_api_error(self):
        session = self.FakeSession(pages_ok=False)
        with self.assertRaises(RuntimeError) as ctx:
            douyin_transcript.push_to_notion(
                session, self._args(), title="x", author="", video_id="1",
                paragraphs=["p"])
        self.assertIn("404", str(ctx.exception))


class StatsTest(unittest.TestCase):
    """互动数据提取与记录。"""

    def test_extract_stats_from_detail(self):
        detail = {"statistics": {"digg_count": 54049, "comment_count": 2905,
                                 "collect_count": 11306, "share_count": 43135,
                                 "play_count": 0}}
        stats = douyin_transcript.extract_stats(detail)
        self.assertEqual(stats, {"digg": 54049, "comment": 2905,
                                 "collect": 11306, "share": 43135})

    def test_extract_stats_missing_is_zero(self):
        self.assertEqual(douyin_transcript.extract_stats({}),
                         {"digg": 0, "comment": 0, "collect": 0, "share": 0})
        self.assertEqual(douyin_transcript.extract_stats({"statistics": None}),
                         {"digg": 0, "comment": 0, "collect": 0, "share": 0})

    def test_stats_line_format(self):
        line = douyin_transcript.stats_line({"digg": 1, "comment": 2, "collect": 3, "share": 4})
        self.assertEqual(line, "- 互动：点赞 1 · 评论 2 · 收藏 3 · 分享 4")

    def test_write_outputs_appends_stats_record(self):
        import argparse, tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(with_meta=True, srt=False)
            txt = Path(tmp) / "视频.txt"
            segments = [(0.0, 1.0, "你好")]
            stats = {"digg": 10, "comment": 20, "collect": 30, "share": 40}
            douyin_transcript._write_outputs(
                txt, args, segments, "标题", "作者", "123", "https://x", stats)
            content = txt.read_text(encoding="utf-8")
            self.assertIn("- 互动：点赞 10 · 评论 20 · 收藏 30 · 分享 40", content)
            record = json.loads((Path(tmp) / "video_stats.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(record["digg"], 10)
            self.assertEqual(record["video_id"], "123")
            self.assertIn("fetched_at", record)

    def test_write_outputs_without_stats_no_record(self):
        import argparse, tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(with_meta=False, srt=False)
            txt = Path(tmp) / "视频.txt"
            douyin_transcript._write_outputs(
                txt, args, [(0.0, 1.0, "你好")], "标题", "", "123", "https://x", None)
            self.assertFalse((Path(tmp) / "video_stats.jsonl").exists())
            self.assertNotIn("互动", txt.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
