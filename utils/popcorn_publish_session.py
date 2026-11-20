"""Browser binding for the publish-only human-control protocol."""
import asyncio
import http.client
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

_OBSERVER = r"""(requiresPermit = false) => {
  if (window.__popcornPublishObserver) return;
  window.__popcornPublishObserver = true;
  let approved = !requiresPermit;
  let requesting = false;
  let replaying = null;
  window.__popcornAllowAutomatedPublish = () => { approved = true; };
  document.addEventListener('click', event => {
    const button = event.target.closest('button,[role="button"]');
    const label = button?.innerText?.trim();
    const confirm = button?.matches('.ant-modal-confirm-centered button.ant-btn-primary');
    if (!confirm && !['发布', '发表', '定时发布', '立即发布'].includes(label)) return;
    if (button === replaying) return;
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
    if (!approved) {
      event.preventDefault();
      event.stopImmediatePropagation();
      if (requesting || !event.isTrusted) return;
      requesting = true;
      void window.__popcornRequestPublishAdmission(click).then(allowed => {
        if (!allowed || !button.isConnected) return;
        approved = true;
        button.setAttribute('data-popcorn-submit-clicked', 'true');
        document.documentElement.setAttribute('data-popcorn-submit-click', JSON.stringify(click));
        replaying = button;
        try { button.click(); } finally { replaying = null; }
      }).catch(() => {}).finally(() => { requesting = false; });
      return;
    }
    button.setAttribute('data-popcorn-submit-clicked', 'true');
    // DOM attributes are visible across Patchright's isolated evaluation worlds.
    document.documentElement.setAttribute('data-popcorn-submit-click', JSON.stringify(click));
    window.__popcornPublishClicked(click);
  }, true);
}"""


class BrowserPublishSession:
    def __init__(self, page, *, headed, platform, control_file, admission_url=None, admission_token=None):
        self.page = page
        self.platform = platform
        self.manual_click = None
        self.control_file = Path(control_file)
        self.admission_url = admission_url
        self.admission_token = admission_token
        self.admission_lock = asyncio.Lock()
        self.admission_granted = False
        self.admission_denied = False
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
        if self.admission_url:
            await self.page.expose_binding('__popcornRequestPublishAdmission', self.admit_manual_click)
        observer = f'({_OBSERVER})({json.dumps(bool(self.admission_url))})'
        await self.page.add_init_script(observer)
        await self.page.evaluate(observer)
        if self.interaction.headed:
            # Headed pages are visible before a blocker is detected, including debug mode.
            _emit({'type': 'checkpoint', 'checkpoint': 'manual_control'})

    async def request_admission(self):
        endpoint = urlparse(self.admission_url)
        if (endpoint.scheme != 'http' or endpoint.hostname != '127.0.0.1'
                or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment
                or endpoint.path != '/api/desktop/publish-admission' or not self.admission_token):
            return False

        def request():
            connection = http.client.HTTPConnection('127.0.0.1', endpoint.port, timeout=2)
            try:
                connection.request('POST', endpoint.path, headers={'Authorization': f'Bearer {self.admission_token}'})
                return connection.getresponse().status == 204
            except (OSError, http.client.HTTPException):
                return False
            finally:
                connection.close()

        return await asyncio.to_thread(request)

    async def acquire_admission(self):
        if not self.admission_url:
            return True
        async with self.admission_lock:
            if self.admission_granted or self.admission_denied:
                return False
            self.admission_granted = await self.request_admission()
            if not self.admission_granted:
                self.admission_denied = True
                _emit({'type': 'publish_admission_rejected'})
            return self.admission_granted

    async def admit_manual_click(self, source, click):
        allowed_host = {'douyin': 'creator.douyin.com', 'kuaishou': 'cp.kuaishou.com',
                        'wechat_channels': 'channels.weixin.qq.com', 'xiaohongshu': 'creator.xiaohongshu.com'}
        if source['frame'] != self.page.main_frame or urlparse(source['frame'].url).hostname != allowed_host.get(self.platform):
            return False
        if not await self.acquire_admission():
            return False
        self.manual_click = click
        _emit({'type': 'checkpoint', 'checkpoint': 'submitting'})
        return True

    async def control(self):
        try:
            data = json.loads(self.control_file.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return None
        self.control_file.unlink(missing_ok=True)
        return data

    async def probe(self):
        if self.admission_denied:
            raise RuntimeError('未取得发布提交许可')
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
        if self.interaction.headed:
            await self.interaction.check_result()
            if self.manual_click is not None:
                # Once a human has submitted, only observe that attempt.
                await self.interaction.wait_for_user('已检测到人工提交，正在核对平台结果')
                await self.interaction.check_result()
                raise ManualPublishUncertain('人工提交结果待确认')
        if not await self.acquire_admission():
            raise RuntimeError('未取得发布提交许可')
        if self.admission_url and self.interaction.headed:
            await self.page.evaluate("() => window.__popcornAllowAutomatedPublish()")
        self.interaction.submitting = True


@asynccontextmanager
async def publish_session(page, *, headed, platform):
    control_file = os.environ.get('POPCORN_SAU_CONTROL_FILE', '').strip()
    if not control_file:
        yield
        return
    session = BrowserPublishSession(
        page, headed=headed, platform=platform, control_file=control_file,
        admission_url=os.environ.get('POPCORN_SAU_PUBLISH_ADMISSION_URL'),
        admission_token=os.environ.get('POPCORN_SAU_PUBLISH_ADMISSION_TOKEN'),
    )
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
