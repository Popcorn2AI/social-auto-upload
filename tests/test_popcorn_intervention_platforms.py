import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uploader.douyin_uploader.main import DouYinVideo, DouYinNote
from uploader.ks_uploader.main import KSVideo, KSNote
from uploader.tencent_uploader.main import TencentVideo
from uploader.xiaohongshu_uploader.main import XiaoHongShuVideo, XiaoHongShuNote


class PlatformInterventionTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_supported_publish_flows_report_pre_submit_page_blockers(self):
        factories = [
            lambda: DouYinVideo('标题', '/tmp/video', [], 0, '/tmp/cookie', headless=True),
            lambda: DouYinNote(['/tmp/image'], '正文', [], 0, '/tmp/cookie', headless=True),
            lambda: KSVideo('标题', '/tmp/video', [], 0, '/tmp/cookie', headless=True),
            lambda: KSNote(['/tmp/image'], '正文', [], 0, '/tmp/cookie', headless=True),
            lambda: TencentVideo('标题', '/tmp/video', [], 0, '/tmp/cookie', headless=True),
            lambda: XiaoHongShuVideo('标题', '/tmp/video', [], 0, '/tmp/cookie', headless=True),
            lambda: XiaoHongShuNote(['/tmp/image'], '正文', [], 0, '/tmp/cookie', headless=True),
        ]
        for factory in factories:
            app = factory()
            with self.subTest(platform=type(app).__name__):
                app.validate_upload_args = AsyncMock()
                page = MagicMock()
                page.is_closed.return_value = False
                page.expose_function = AsyncMock()
                page.add_init_script = AsyncMock()
                page.evaluate = AsyncMock(return_value=None)
                page.goto = AsyncMock(side_effect=TimeoutError('missing publish page'))
                app.open_upload_page = AsyncMock(side_effect=TimeoutError('missing publish page'))
                context = MagicMock()
                context.new_page = AsyncMock(return_value=page)
                context.close = AsyncMock()
                browser = MagicMock()
                browser.new_context = AsyncMock(return_value=context)
                browser.close = AsyncMock()
                playwright = MagicMock()
                playwright.chromium.launch = AsyncMock(return_value=browser)
                module = type(app).__module__
                events = []
                with patch.dict(os.environ, {'POPCORN_SAU_CONTROL_FILE': '/tmp/unused'}), \
                     patch(module + '.set_init_script', AsyncMock(return_value=context), create=True), \
                     patch(module + '.capture_page_diagnostic', AsyncMock()), \
                     patch('utils.popcorn_publish_session._emit', events.append):
                    with self.assertRaises(TimeoutError):
                        await app.upload(playwright)
                self.assertTrue(any(event.get('kind') == 'page_blocked' for event in events), events)


if __name__ == '__main__':
    unittest.main()
