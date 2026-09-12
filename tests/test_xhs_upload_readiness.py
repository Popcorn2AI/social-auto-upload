import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from uploader.xiaohongshu_uploader.main import XiaoHongShuVideo


class ReachedMetadata(Exception):
    pass


class UploadReadinessTests(unittest.TestCase):
    def test_title_visible_at_35_percent_does_not_start_cover_flow(self):
        app = XiaoHongShuVideo('title', '/tmp/video.mp4', [], 0, '/tmp/cookie.json')
        page = MagicMock()
        page.goto = AsyncMock()
        page.wait_for_url = AsyncMock()
        locator = MagicMock()
        locator.set_input_files = AsyncMock()
        locator.count = AsyncMock(return_value=1)
        locator.is_visible = AsyncMock(return_value=True)
        page.locator.return_value = locator
        upload_input = MagicMock()
        upload_input.query_selector = AsyncMock(return_value=None)
        page.wait_for_selector = AsyncMock(return_value=upload_input)
        page.evaluate = AsyncMock(side_effect=['uploading', 'complete'])
        app.fill_meta = AsyncMock(side_effect=ReachedMetadata)
        app.set_thumbnail = AsyncMock()
        with patch('uploader.xiaohongshu_uploader.main.asyncio.sleep', new_callable=AsyncMock) as sleep:
            with self.assertRaises(ReachedMetadata):
                asyncio.run(app.upload_video_content(page))
        self.assertEqual(page.evaluate.await_count, 2)
        sleep.assert_awaited_once_with(2)
        app.set_thumbnail.assert_not_awaited()

    def test_upload_failure_stops_before_metadata_and_cover(self):
        app = XiaoHongShuVideo('title', '/tmp/video.mp4', [], 0, '/tmp/cookie.json')
        page = MagicMock()
        page.goto = AsyncMock()
        page.wait_for_url = AsyncMock()
        page.locator.return_value.set_input_files = AsyncMock()
        page.evaluate = AsyncMock(return_value='failed')
        app.fill_meta = AsyncMock()
        app.set_thumbnail = AsyncMock()
        with self.assertRaisesRegex(RuntimeError, '视频上传失败'):
            asyncio.run(app.upload_video_content(page))
        app.fill_meta.assert_not_awaited()
        app.set_thumbnail.assert_not_awaited()

    def test_unknown_upload_state_times_out_without_setting_cover(self):
        app = XiaoHongShuVideo('title', '/tmp/video.mp4', [], 0, '/tmp/cookie.json')
        page = MagicMock()
        page.goto = AsyncMock()
        page.wait_for_url = AsyncMock()
        page.locator.return_value.set_input_files = AsyncMock()
        page.evaluate = AsyncMock(return_value='unknown')
        app.set_thumbnail = AsyncMock()
        with patch('uploader.xiaohongshu_uploader.main.asyncio.sleep', new_callable=AsyncMock):
            with self.assertRaisesRegex(TimeoutError, '未开始设置封面'):
                asyncio.run(app.upload_video_content(page))
        app.set_thumbnail.assert_not_awaited()
