import asyncio
import os
import unittest
from unittest.mock import AsyncMock, patch
from utils.popcorn_publish_session import publish_session, publish_step, before_publish_submit


class FakePage:
    url = 'https://creator.example/publish'
    async def expose_function(self, name, callback):
        self.callback = callback
    async def add_init_script(self, script):
        pass
    async def evaluate(self, script):
        return None
    def is_closed(self):
        return False


class PublishSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_manual_platform_success_unwinds_without_automatic_submit(self):
        for platform in ('douyin', 'kuaishou', 'wechat_channels', 'xiaohongshu'):
            for mode in ('published', 'scheduled'):
                with self.subTest(platform=platform, mode=mode):
                    page = FakePage()
                    hosts = {'douyin': 'creator.douyin.com', 'kuaishou': 'cp.kuaishou.com', 'wechat_channels': 'channels.weixin.qq.com', 'xiaohongshu': 'creator.xiaohongshu.com'}
                    page.url = 'https://' + hosts[platform] + '/publish'
                    events, calls = [], []
                    async def evaluate(script):
                        if "getAttribute('data-popcorn-submit-click')" in script:
                            return {'mode': mode} if calls else None
                        if "querySelectorAll('[role" in script:
                            return 'success' if calls else None
                        return None
                    page.evaluate = evaluate
                    async def blocked():
                        calls.append('prepare')
                        raise TimeoutError('button missing')
                    with patch.dict(os.environ, {'POPCORN_SAU_CONTROL_FILE': '/tmp/not-used'}), patch('utils.popcorn_publish_session._emit', events.append):
                        async with publish_session(page, headed=True, platform=platform):
                            await publish_step(blocked)
                            await before_publish_submit()
                            calls.append('automatic submit')
                    self.assertEqual(calls, ['prepare'])
                    self.assertEqual(events[-1], {'type': 'result', 'status': mode})

    async def test_manual_success_interrupts_an_in_progress_automation_step(self):
        page = FakePage()
        page.url = 'https://creator.douyin.com/creator-micro/content/publish'
        events, calls = [], []
        async def evaluate(script):
            if "getAttribute('data-popcorn-submit-click')" in script:
                return {'mode': 'published'} if calls else None
            if "querySelectorAll('[role" in script:
                return 'success' if calls else None
            return None
        page.evaluate = evaluate
        async def long_step():
            calls.append('started')
            try:
                await asyncio.sleep(10)
            finally:
                calls.append('stopped')
        async def flow():
            async with publish_session(page, headed=True, platform='douyin'):
                await publish_step(long_step)
                calls.append('automatic submission')
        with patch.dict(os.environ, {'POPCORN_SAU_CONTROL_FILE': '/tmp/not-used'}), patch('utils.popcorn_publish_session._emit', events.append):
            await asyncio.wait_for(flow(), timeout=.5)
        self.assertEqual(calls, ['started', 'stopped'])
        self.assertEqual(events[-1], {'type': 'result', 'status': 'published'})

    async def test_explicit_publish_redirect_after_manual_click_is_success(self):
        for platform, url in (
            ('douyin', 'https://creator.douyin.com/creator-micro/content/manage?enter_from=publish'),
            ('kuaishou', 'https://cp.kuaishou.com/article/manage/video?status=2&from=publish'),
        ):
            page = FakePage()
            page.url = url
            events = []
            async def evaluate(script):
                return {'mode': 'published'} if "getAttribute('data-popcorn-submit-click')" in script else None
            page.evaluate = evaluate
            async def flow():
                async with publish_session(page, headed=True, platform=platform):
                    await publish_step(AsyncMock())
            with patch.dict(os.environ, {'POPCORN_SAU_CONTROL_FILE': '/tmp/not-used'}), patch('utils.popcorn_publish_session._emit', events.append):
                await asyncio.wait_for(flow(), timeout=.2)
            self.assertEqual(events[-1], {'type': 'result', 'status': 'published'})

    async def test_other_origin_success_is_not_this_platform_result(self):
        page = FakePage()
        events = []
        async def evaluate(script):
            if "getAttribute('data-popcorn-submit-click')" in script:
                return {'mode': 'published'}
            if "querySelectorAll('[role" in script:
                return 'success'
            return None
        page.evaluate = evaluate
        with patch.dict(os.environ, {'POPCORN_SAU_CONTROL_FILE': '/tmp/not-used'}), patch('utils.popcorn_publish_session._emit', events.append):
            async with publish_session(page, headed=True, platform='douyin'):
                await publish_step(AsyncMock(return_value='prepared'))
        self.assertFalse(any(event.get('type') == 'result' for event in events))

    async def test_visiting_manage_page_without_submit_is_not_success(self):
        page = FakePage()
        page.url = 'https://creator.douyin.com/creator-micro/content/manage'
        events = []
        with patch.dict(os.environ, {'POPCORN_SAU_CONTROL_FILE': '/tmp/not-used'}), patch('utils.popcorn_publish_session._emit', events.append):
            async with publish_session(page, headed=True, platform='douyin'):
                await publish_step(AsyncMock())
        self.assertFalse(any(event.get('type') == 'result' for event in events))

    async def test_automatic_submission_cannot_be_clicked_twice(self):
        page = FakePage()
        events = []
        calls = []
        with patch.dict(os.environ, {'POPCORN_SAU_CONTROL_FILE': '/tmp/not-used'}), patch('utils.popcorn_publish_session._emit', events.append):
            with self.assertRaisesRegex(RuntimeError, '结果待确认'):
                async with publish_session(page, headed=True, platform='douyin'):
                    await before_publish_submit()
                    calls.append('click')
                    await before_publish_submit()
                    calls.append('duplicate')
        self.assertEqual(calls, ['click'])
        self.assertEqual(events[0]['checkpoint'], 'manual_control')

    async def test_headless_flow_does_not_install_human_page_observers(self):
        page = FakePage()
        page.evaluate = AsyncMock(side_effect=AssertionError('headless must not install human probes'))
        with patch.dict(os.environ, {'POPCORN_SAU_CONTROL_FILE': '/tmp/not-used'}):
            async with publish_session(page, headed=False, platform='douyin'):
                self.assertEqual(await publish_step(AsyncMock(return_value='ready')), 'ready')
                await before_publish_submit()
        page.evaluate.assert_not_awaited()

    async def test_without_control_contract_keeps_legacy_behavior(self):
        with patch.dict(os.environ, {'POPCORN_SAU_CONTROL_FILE': ''}):
            async with publish_session(FakePage(), headed=False, platform='douyin'):
                operation = AsyncMock(return_value='ok')
                self.assertEqual(await publish_step(operation), 'ok')
                await before_publish_submit()


if __name__ == '__main__':
    unittest.main()
