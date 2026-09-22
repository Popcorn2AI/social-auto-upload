"""Explicit local Chromium smoke. Every network request is fulfilled by this fixture.

Run with a prepared Runtime's Python and PLAYWRIGHT_BROWSERS_PATH. No accounts,
cookies, platform requests or external publishing are used.
"""
import asyncio
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch
from patchright.async_api import async_playwright
from utils.popcorn_publish_session import publish_session, publish_step, before_publish_submit


async def check(browser, case):
    events, operations, automatic_submissions = [], [], []
    with tempfile.TemporaryDirectory() as directory:
        control = Path(directory) / 'control'
        context = await browser.new_context()
        page = await context.new_page()
        label = '定时发布' if case == 'scheduled' else '立即发布'
        body = f'''<html><meta charset="utf-8"><body>
          <label><input type="radio" checked name="mode">{label}</label>
          <button onclick="this.dataset.clicks=String(Number(this.dataset.clicks||0)+1);document.querySelector('#result').innerText='发布成功'">发布</button>
          <div id="result" role="alert"></div></body></html>'''
        await page.route('**/*', lambda route: route.fulfill(status=200,
            content_type='text/html; charset=utf-8', body=body))

        async def user():
            for _ in range(100):
                waiting = next((event for event in reversed(events) if event.get('state') == 'waiting'), None)
                if waiting:
                    if case == 'continue':
                        control.write_text(json.dumps({'sessionId': waiting['sessionId'], 'action': 'continue'}))
                    else:
                        button = page.get_by_role('button', name='发布', exact=True)
                        await button.click()
                        await button.click()
                        assert await button.get_attribute('data-clicks') == '1', 'duplicate DOM submit'
                    return
                await asyncio.sleep(.05)
            raise AssertionError('never entered human wait')

        async def blocked_operation():
            operations.append('prepare')
            if len(operations) == 1:
                raise TimeoutError('local simulated page blocker')

        actor = asyncio.create_task(user())
        try:
            with patch.dict(os.environ, {'POPCORN_SAU_CONTROL_FILE': str(control)}), \
                 patch('utils.popcorn_publish_session._emit', events.append):
                async with publish_session(page, headed=True, platform='douyin'):
                    await page.goto('https://creator.douyin.com/creator-micro/content/publish')
                    await publish_step(blocked_operation)
                    await before_publish_submit()
                    automatic_submissions.append('submit')
                await actor
            if case == 'continue':
                assert operations == ['prepare', 'prepare']
                assert automatic_submissions == ['submit']
            else:
                assert automatic_submissions == [], automatic_submissions
                assert events[-1] == {'type': 'result', 'status': case}, events
            print(f'PASS real headed Chromium: {case}', flush=True)
        finally:
            actor.cancel()
            await asyncio.gather(actor, return_exceptions=True)
            await context.close()


async def main():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=False, channel='chromium')
        try:
            for case in ('published', 'scheduled', 'continue'):
                await asyncio.wait_for(check(browser, case), timeout=20)
        finally:
            await browser.close()


if __name__ == '__main__':
    asyncio.run(main())
