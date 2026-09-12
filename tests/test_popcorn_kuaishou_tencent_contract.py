import asyncio
import inspect
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import sau_cli
from uploader.ks_uploader.main import KSNote, KSVideo, build_kuaishou_publish_text
from uploader.tencent_uploader.main import TencentVideo, build_tencent_description_text


class PopcornKuaishouTencentContractTests(unittest.TestCase):
    def test_tencent_description_contains_intro_then_tags_without_repeating_title(self):
        self.assertEqual(
            build_tencent_description_text("作品简介", ["话题一", "#话题二"]),
            "作品简介\n#话题一 #话题二",
        )

    def test_kuaishou_publish_text_preserves_title_then_description_and_tags(self):
        self.assertEqual(
            build_kuaishou_publish_text("完整标题", "作品简介", ["话题一", "话题二"]),
            "完整标题\n作品简介\n#话题一 #话题二",
        )
        self.assertEqual(
            build_kuaishou_publish_text("重复内容", "重复内容", ["话题"]),
            "重复内容\n#话题",
        )

    def test_kuaishou_publish_text_rejects_overflow_instead_of_truncating(self):
        with self.assertRaisesRegex(ValueError, "上限"):
            build_kuaishou_publish_text("必须完整保留的标题", "很长的简介内容", [], max_length=12)

    def test_kuaishou_publish_text_rejects_excess_tags_instead_of_dropping_them(self):
        with self.assertRaisesRegex(ValueError, "话题不能超过 3 个"):
            build_kuaishou_publish_text("标题", "正文", ["一", "二", "三", "四"])

    def test_kuaishou_note_text_contains_title_body_and_tags(self):
        self.assertEqual(
            build_kuaishou_publish_text("图文标题", "图文正文", ["话题一", "#话题二"]),
            "图文标题\n图文正文\n#话题一 #话题二",
        )

    def test_tencent_description_refocuses_and_verifies_editor_content(self):
        app = TencentVideo(
            "短标题", "/tmp/video.mp4", ["话题"], 0, "/tmp/cookie.json",
            desc="作品简介",
        )
        editor = MagicMock()
        editor.first = editor
        editor.click = AsyncMock(return_value=None)
        editor.inner_text = AsyncMock(return_value="作品简介\n#话题")
        page = MagicMock()
        page.locator.return_value = editor
        page.keyboard.press = AsyncMock(return_value=None)
        page.keyboard.type = AsyncMock(return_value=None)

        asyncio.run(app.fill_description(page))

        editor.click.assert_awaited()
        editor.inner_text.assert_awaited()
        page.keyboard.type.assert_awaited_once_with("作品简介\n#话题")

    def test_parser_accepts_explicit_compliance_options(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            video = Path(temp_dir) / "video.mp4"
            image = Path(temp_dir) / "cover.png"
            video.write_bytes(b"video")
            image.write_bytes(b"image")
            parser = sau_cli.build_parser()

            kuaishou_video = parser.parse_args([
                "kuaishou", "upload-video", "--account", "main", "--file", str(video),
                "--title", "title", "--declare-original",
            ])
            kuaishou_note = parser.parse_args([
                "kuaishou", "upload-note", "--account", "main", "--images", str(image),
                "--title", "title", "--declare-original",
            ])
            tencent_video = parser.parse_args([
                "tencent", "upload-video", "--account", "main", "--file", str(video),
                "--title", "title", "--declare-original", "--content-label", "ai_generated",
            ])

        self.assertTrue(kuaishou_video.declare_original)
        self.assertTrue(kuaishou_note.declare_original)
        self.assertTrue(tencent_video.declare_original)
        self.assertEqual(tencent_video.content_label, "ai_generated")

    def test_unspecified_compliance_never_touches_platform_controls(self):
        kuaishou = KSVideo("title", "/tmp/video.mp4", [], 0, "/tmp/cookie.json")
        tencent = TencentVideo("title", "/tmp/video.mp4", [], 0, "/tmp/cookie.json")
        page = MagicMock()

        asyncio.run(kuaishou.apply_original_declaration(page))
        asyncio.run(tencent.apply_original_statement(page))

        self.assertEqual(page.mock_calls, [])

    def test_explicit_compliance_failure_blocks_target(self):
        missing = MagicMock()
        missing.first = missing
        missing.count = AsyncMock(return_value=0)
        page = MagicMock()
        page.locator.return_value = missing
        page.get_by_text.return_value = missing

        kuaishou = KSNote(
            ["/tmp/cover.png"], "note", [], 0, "/tmp/cookie.json",
            declare_original=True,
        )
        with self.assertRaisesRegex(RuntimeError, "原创声明设置失败"):
            asyncio.run(kuaishou.apply_original_declaration(page))

        tencent = TencentVideo(
            "title", "/tmp/video.mp4", [], 0, "/tmp/cookie.json",
            content_label="ai_generated",
        )
        with self.assertRaisesRegex(RuntimeError, "内容标注设置失败"):
            asyncio.run(tencent.apply_original_statement(page))

    def test_supported_uploaders_emit_structured_submission_lifecycle(self):
        kuaishou_video_source = inspect.getsource(KSVideo.upload)
        kuaishou_note_source = inspect.getsource(KSNote.upload_note_content)
        tencent_source = inspect.getsource(TencentVideo.upload)
        for source in (kuaishou_video_source, kuaishou_note_source, tencent_source):
            self.assertIn('emit_checkpoint("submitting")', source)
            self.assertIn("emit_result(", source)

    def test_dispatch_propagates_compliance_without_implicit_defaults(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            video = Path(temp_dir) / "video.mp4"
            video.write_bytes(b"video")
            parser = sau_cli.build_parser()
            explicit = parser.parse_args([
                "tencent", "upload-video", "--account", "main", "--file", str(video),
                "--title", "title", "--declare-original", "--content-label", "ai_generated",
            ])
            implicit = parser.parse_args([
                "tencent", "upload-video", "--account", "main", "--file", str(video),
                "--title", "title",
            ])

            upload = AsyncMock(return_value=Path("cookie.json"))
            with patch("sau_cli.upload_tencent_video", new=upload):
                asyncio.run(sau_cli.dispatch(explicit))
                asyncio.run(sau_cli.dispatch(implicit))

        explicit_request = upload.await_args_list[0].args[0]
        implicit_request = upload.await_args_list[1].args[0]
        self.assertTrue(explicit_request.declare_original)
        self.assertEqual(explicit_request.content_label, "ai_generated")
        self.assertFalse(implicit_request.declare_original)
        self.assertIsNone(implicit_request.content_label)


if __name__ == "__main__":
    unittest.main()
