import asyncio
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from uploader.douyin_uploader.main import DouYinVideo
from uploader.ks_uploader.main import KSVideo, submit_kuaishou_publish_once
from uploader.tencent_uploader.main import TencentBaseUploader, TencentVideo
from uploader.xiaohongshu_uploader.main import XiaoHongShuVideo


def _locator(*, count: int = 1, visible: bool = True, value: str = "", text: str = ""):
    locator = MagicMock()
    locator.first = locator
    locator.last = locator
    locator.filter.return_value = locator
    locator.nth.return_value = locator
    locator.locator.return_value = locator
    locator.get_by_role.return_value = locator
    locator.get_by_text.return_value = locator
    locator.count = AsyncMock(return_value=count)
    locator.is_visible = AsyncMock(return_value=visible)
    locator.wait_for = AsyncMock(return_value=None)
    locator.click = AsyncMock(return_value=None)
    locator.hover = AsyncMock(return_value=None)
    locator.fill = AsyncMock(return_value=None)
    locator.press = AsyncMock(return_value=None)
    locator.input_value = AsyncMock(return_value=value)
    locator.inner_text = AsyncMock(return_value=text)
    locator.get_attribute = AsyncMock(return_value="")
    locator.set_input_files = AsyncMock(return_value=None)
    return locator


class PopcornPublishIntegrityTests(unittest.TestCase):
    def test_douyin_headless_publish_verification_requests_visible_retry(self):
        app = DouYinVideo("标题", "/tmp/video.mp4", [], 0, "/tmp/cookie.json", headless=True)
        sms_input = _locator()

        with self.assertRaisesRegex(RuntimeError, "可见浏览器"):
            asyncio.run(app.handle_publish_verification(sms_input))

    def test_tencent_headless_verification_requests_visible_retry(self):
        app = TencentBaseUploader(publish_date=0, account_file="/tmp/cookie.json", headless=True)
        dialog = _locator()
        page = MagicMock()
        page.locator.return_value = dialog

        with self.assertRaisesRegex(RuntimeError, "可见浏览器"):
            asyncio.run(app.wait_for_realtime_verification(page))

    def test_douyin_provided_cover_blocks_when_cover_dialog_cannot_open(self):
        app = DouYinVideo(
            "标题",
            "/tmp/video.mp4",
            [],
            0,
            "/tmp/cookie.json",
            thumbnail_portrait_path="/tmp/cover.png",
        )
        cover_area = _locator()
        cover_dialog = _locator(count=0, visible=False)
        missing_trigger = _locator(count=0, visible=False)
        page = MagicMock()
        page.locator.side_effect = lambda selector: (
            cover_dialog if selector == "div.dy-creator-content-modal" else cover_area
        )
        page.get_by_text.return_value = missing_trigger
        page.evaluate = AsyncMock(return_value=None)
        page.wait_for_timeout = AsyncMock(return_value=None)
        page.wait_for_selector = AsyncMock(side_effect=TimeoutError("dialog missing"))

        with patch(
            "uploader.douyin_uploader.main._native_click",
            new=AsyncMock(return_value=None),
        ):
            with self.assertRaisesRegex(RuntimeError, "封面弹窗"):
                asyncio.run(app.set_thumbnail(page))

    def test_douyin_title_overflow_is_rejected_instead_of_truncated(self):
        app = DouYinVideo("标题", "/tmp/video.mp4", [], 0, "/tmp/cookie.json")
        page = MagicMock()
        page.locator.return_value = _locator()
        page.keyboard.press = AsyncMock(return_value=None)
        page.keyboard.type = AsyncMock(return_value=None)

        with self.assertRaisesRegex(ValueError, "标题"):
            asyncio.run(app.fill_title_and_description(page, "长" * 31, "简介", []))

        self.assertEqual(page.mock_calls, [])

    def test_douyin_copy_readback_mismatch_blocks_publish(self):
        app = DouYinVideo("标题", "/tmp/video.mp4", [], 0, "/tmp/cookie.json")
        title = _locator(value="错误标题")
        description = _locator(text="简介")
        page = MagicMock()
        page.locator.side_effect = lambda selector: (
            title if "填写作品标题" in selector else description
        )
        page.keyboard.press = AsyncMock(return_value=None)
        page.keyboard.type = AsyncMock(return_value=None)

        with self.assertRaisesRegex(RuntimeError, "写入校验"):
            asyncio.run(app.fill_title_and_description(page, "正确标题", "简介", []))

    def test_tencent_provided_cover_blocks_when_editor_is_missing(self):
        app = TencentVideo("短标题内容", "/tmp/video.mp4", [], 0, "/tmp/cookie.json")
        app.open_thumbnail_dialog = AsyncMock(return_value=None)

        with self.assertRaisesRegex(RuntimeError, "3:4 竖版封面"):
            asyncio.run(
                app.set_single_thumbnail(
                    MagicMock(),
                    "/tmp/cover.png",
                    ["selector"],
                    ["编辑封面"],
                    "3:4 竖版",
                )
            )

    def test_tencent_short_title_overflow_is_rejected_instead_of_rewritten(self):
        app = TencentVideo("标题", "/tmp/video.mp4", [], 0, "/tmp/cookie.json")
        page = MagicMock()
        page.locator.return_value = _locator()

        with self.assertRaisesRegex(ValueError, "短标题"):
            asyncio.run(app.set_short_title(page, "标题", "过长" * 8))

        self.assertEqual(page.mock_calls, [])

    def test_tencent_short_title_readback_mismatch_blocks_publish(self):
        app = TencentVideo("标题", "/tmp/video.mp4", [], 0, "/tmp/cookie.json")
        page = MagicMock()
        page.locator.return_value = _locator(value="错误短标题")

        with self.assertRaisesRegex(RuntimeError, "写入校验"):
            asyncio.run(app.set_short_title(page, "标题", "视频号独立短标题"))

    def test_tencent_login_redirect_is_not_publish_success(self):
        app = TencentVideo("短标题内容", "/tmp/video.mp4", [], 0, "/tmp/cookie.json")
        publish_button = _locator()
        page = MagicMock()
        page.url = "https://channels.weixin.qq.com/login"
        page.get_by_role.return_value = publish_button
        page.evaluate = AsyncMock(return_value=None)

        with patch(
            "uploader.tencent_uploader.main.asyncio.sleep",
            new=AsyncMock(return_value=None),
        ):
            with self.assertRaisesRegex(RuntimeError, "发布"):
                asyncio.run(app.submit_publish(page))

    def test_douyin_schedule_readback_mismatch_blocks_publish(self):
        app = DouYinVideo("标题", "/tmp/video.mp4", [], 0, "/tmp/cookie.json")
        switch = _locator()
        time_input = _locator(value="2026-09-20 09:00")
        page = MagicMock()
        page.locator.side_effect = lambda selector: (
            time_input if "日期和时间" in selector else switch
        )
        page.keyboard.press = AsyncMock(return_value=None)
        page.keyboard.type = AsyncMock(return_value=None)

        with patch("uploader.douyin_uploader.main.asyncio.sleep", new=AsyncMock()):
            with self.assertRaisesRegex(RuntimeError, "定时"):
                asyncio.run(app.set_schedule_time_douyin(page, datetime(2026, 9, 20, 9, 37)))

    def test_xiaohongshu_schedule_readback_mismatch_blocks_publish(self):
        app = XiaoHongShuVideo("标题", "/tmp/video.mp4", [], 0, "/tmp/cookie.json")
        switch = _locator()
        time_input = _locator(value="2026-09-20 09:00")
        page = MagicMock()
        page.locator.side_effect = lambda selector: (
            time_input if "datepicker" in selector else switch
        )

        with patch("uploader.xiaohongshu_uploader.main.asyncio.sleep", new=AsyncMock()):
            with self.assertRaisesRegex(RuntimeError, "定时"):
                asyncio.run(app.set_schedule_time_xiaohongshu(page, datetime(2026, 9, 20, 9, 37)))

    def test_kuaishou_schedule_readback_mismatch_blocks_publish(self):
        app = KSVideo("标题", "/tmp/video.mp4", [], 0, "/tmp/cookie.json")
        picker = _locator(value="2026-09-20 09:00:00")
        page = MagicMock()
        page.locator.return_value = picker
        page.evaluate = AsyncMock(return_value=True)
        page.keyboard.press = AsyncMock(return_value=None)

        with patch("uploader.ks_uploader.main.asyncio.sleep", new=AsyncMock()):
            with self.assertRaisesRegex(RuntimeError, "定时"):
                asyncio.run(app.set_schedule_time(page, datetime(2026, 9, 20, 9, 37)))

    def test_kuaishou_submit_failure_never_clicks_publish_twice(self):
        modal = _locator(count=0)
        publish_button = _locator()
        page = MagicMock()
        page.locator.return_value = modal
        page.get_by_text.return_value = publish_button
        page.wait_for_url = AsyncMock(side_effect=TimeoutError("result missing"))

        with patch("uploader.ks_uploader.main.asyncio.sleep", new=AsyncMock()):
            with self.assertRaisesRegex(RuntimeError, "待确认"):
                asyncio.run(submit_kuaishou_publish_once(page))

        publish_button.click.assert_awaited_once()

    def test_tencent_schedule_keeps_minutes_and_verifies_value(self):
        app = TencentVideo("标题", "/tmp/video.mp4", [], 0, "/tmp/cookie.json")
        locator = _locator(value="2026-09-20 09:37")
        day = _locator(text="20")
        day.evaluate = AsyncMock(return_value="")
        page = MagicMock()
        page.locator.return_value = locator
        page.click = AsyncMock(return_value=None)
        page.inner_text = AsyncMock(return_value="09月")
        page.query_selector_all = AsyncMock(return_value=[day])
        page.keyboard.press = AsyncMock(return_value=None)
        page.keyboard.type = AsyncMock(return_value=None)
        page.wait_for_timeout = AsyncMock(return_value=None)

        asyncio.run(app.set_schedule_time_tencent(page, datetime(2026, 9, 20, 9, 37)))

        locator.fill.assert_awaited_once_with("09:37")


if __name__ == "__main__":
    unittest.main()
