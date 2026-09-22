"""Browser binding for the publish-only human-control protocol."""
import asyncio
import json
import os
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from utils.popcorn_events import _emit
from utils.popcorn_intervention import PublishIntervention, ManualPublishCompleted, ManualPublishUncertain

_active = ContextVar('popcorn_publish_intervention', default=None)


def publish_session_active():
    return _active.get() is not None

_OBSERVER = r"""() => {
  if (window.__popcornPublishObserver) return;
  window.__popcornPublishObserver = true;
  document.addEventListener('click', event => {
    const button = event.target.closest('button,[role="button"]');
    const label = button?.innerText?.trim();
    if (!['发布', '发表', '定时发布'].includes(label)) return;
    if (button.hasAttribute('data-popcorn-submit-clicked')) {
      event.preventDefault();
      event.stopImmediatePropagation();
      return;
    }
    const selected = [...document.querySelectorAll('input[type="radio"]:checked,[role="radio"][aria-checked="true"]')]
      .map(el => (el.closest('label') || el.parentElement)?.innerText || '').join(' ');
    const mode = label === '定时发布' || /定时/.test(selected) ? 'scheduled'
      : /立即|现在|实时/.test(selected) ? 'published' : null;
    const previous = JSON.parse(document.documentElement.getAttribute('data-popcorn-submit-click') || 'null');
    const click = {mode: mode || previous?.mode || null};
    button.setAttribute('data-popcorn-submit-clicked', 'true');
    // DOM attributes are visible across Patchright's isolated evaluation worlds.
    document.documentElement.setAttribute('data-popcorn-submit-click', JSON.stringify(click));
    window.__popcornPublishClicked(click);
  }, true);
}"""


class BrowserPublishSession:
    def __init__(self, page, *, headed, platform, control_file):
        self.page = page
        self.platform = platform
        self.manual_click = None
        self.control_file = Path(control_file)
        self.interaction = PublishIntervention(
            headed=headed, emit=_emit, probe=self.probe, control=self.control,
            session_id=uuid.uuid4().hex,
        )

    async def install(self):
        if not self.interaction.headed:
            return
        def clicked(data):
            if not self.interaction.submitting:
                self.manual_click = data
        await self.page.expose_function('__popcornPublishClicked', clicked)
        await self.page.add_init_script(f'({_OBSERVER})()')
        await self.page.evaluate(_OBSERVER)
        if self.interaction.headed:
            # Headed pages are visible before a blocker is detected, including debug mode.
            _emit({'type': 'checkpoint', 'checkpoint': 'manual_control'})

    async def control(self):
        try:
            data = json.loads(self.control_file.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return None
        self.control_file.unlink(missing_ok=True)
        return data

    async def probe(self):
        try:
            return await self._probe()
        except Exception as error:
            if 'execution context was destroyed' in str(error).lower() or 'cannot find context' in str(error).lower():
                return None  # Navigation is in flight; the next observation uses the new document.
            raise

    async def _probe(self):
        if self.page.is_closed():
            raise ManualPublishUncertain('浏览器已关闭，发布结果待确认')
        url = urlparse(self.page.url)
        allowed_host = {'douyin': 'creator.douyin.com', 'kuaishou': 'cp.kuaishou.com',
                        'wechat_channels': 'channels.weixin.qq.com', 'xiaohongshu': 'creator.xiaohongshu.com'}
        if url.hostname != allowed_host.get(self.platform):
            return None
        click = await self.page.evaluate("() => JSON.parse(document.documentElement.getAttribute('data-popcorn-submit-click') || 'null')")
        if click and not self.interaction.submitting:
            self.manual_click = click
        if self.manual_click is None:
            return None
        # A user navigating to the work list without clicking publish is not success.
        query = parse_qs(url.query)
        success_route = (
            self.platform == 'xiaohongshu' and url.hostname == 'creator.xiaohongshu.com' and url.path == '/publish/success'
            or self.platform == 'douyin' and url.path == '/creator-micro/content/manage' and query.get('enter_from') == ['publish']
            or self.platform == 'kuaishou' and url.path == '/article/manage/video' and query.get('from') == ['publish'] and query.get('status') == ['2']
        )
        notice = await self.page.evaluate(r"""() => {
          const texts = [...document.querySelectorAll('[role="alert"],.ant-message-notice,.weui-desktop-toast,.semi-toast-content')]
            .filter(el => el.getClientRects().length).map(el => el.innerText.trim());
          if (texts.some(text => /^(定时发布成功|预约发布成功)$/.test(text))) return 'scheduled';
          if (texts.some(text => /^(发布成功|发表成功)$/.test(text))) return 'success';
          return null;
        }""")
        if notice == 'scheduled':
            return 'scheduled'
        if success_route or notice == 'success':
            mode = self.manual_click.get('mode')
            # XHS exposes scheduling in the submit button itself.
            if mode is None and self.platform == 'xiaohongshu':
                mode = 'published'
            return mode
        return None

    async def run_operation(self, operation):
        if not self.interaction.headed:
            return await operation()

        async def observe_manual_publish():
            while True:
                await self.interaction.check_result()
                if self.manual_click is not None:
                    return
                await asyncio.sleep(.1)

        task = asyncio.create_task(operation())
        observer = asyncio.create_task(observe_manual_publish())
        try:
            await asyncio.wait((task, observer), return_when=asyncio.FIRST_COMPLETED)
            if observer.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                observer.result()  # Propagate confirmed success/closed-window signals first.
                await self.before_submit()
                raise ManualPublishUncertain('人工提交结果待确认')
            return task.result()
        finally:
            task.cancel()
            observer.cancel()
            await asyncio.gather(task, observer, return_exceptions=True)

    async def before_submit(self):
        if self.interaction.submitting:
            raise ManualPublishUncertain('已经尝试提交，发布结果待确认')
        if not self.interaction.headed:
            self.interaction.submitting = True
            return
        await self.interaction.check_result()
        if self.manual_click is not None:
            # No automatic submission after any human submit click, even if validation failed.
            await self.interaction.wait_for_user('已检测到人工提交，正在核对平台结果')
            await self.interaction.check_result()
            raise ManualPublishUncertain('人工提交结果待确认')
        self.interaction.submitting = True


@asynccontextmanager
async def publish_session(page, *, headed, platform):
    control_file = os.environ.get('POPCORN_SAU_CONTROL_FILE', '').strip()
    if not control_file:
        yield
        return
    session = BrowserPublishSession(page, headed=headed, platform=platform, control_file=control_file)
    await session.install()
    token = _active.set(session)
    try:
        yield
    except ManualPublishCompleted:
        pass
    except ManualPublishUncertain as error:
        raise RuntimeError(f'发布结果待确认：{error}') from error
    finally:
        _active.reset(token)


async def publish_step(operation):
    session = _active.get()
    if session is None:
        return await operation()
    if not session.interaction.headed:
        return await session.interaction.step(operation)
    await session.interaction.check_result()
    if session.manual_click is not None:
        await session.before_submit()
    return await session.interaction.step(lambda: session.run_operation(operation))


async def before_publish_submit():
    session = _active.get()
    if session is not None:
        await session.before_submit()
