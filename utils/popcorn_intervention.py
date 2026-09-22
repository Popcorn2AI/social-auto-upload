"""Publish-only human control. Platform adapters own success evidence and steps.

A step must be safe to repeat before submission (e.g. setting a form field).
Never wrap an entire upload or a submit click as a repeatable step.
"""
import asyncio
import re
import time
import uuid


class ManualPublishCompleted(BaseException):
    """Unwind automation after an observed platform result, including broad catches."""


class ManualPublishUncertain(BaseException):
    """Stop without repeating anything that the human may already have submitted."""


def blocker_kind(error):
    message = str(error)
    if re.search(r'ERR_(?:CONNECTION|TIMED_OUT|NETWORK)|ECONNRESET|ETIMEDOUT|网络|限流|频繁|封禁|受限', message, re.I):
        return None
    if re.search(r'验证码|真人验证|实名验证|短信验证|安全验证|captcha', message, re.I):
        return 'verification_required'
    if isinstance(error, TimeoutError) or type(error).__name__ == 'TimeoutError':
        return 'page_blocked'
    if re.search(r'未找到.*(?:按钮|编辑|输入)|未能定位|疑似发布页改版|无法打开.*弹窗', message):
        return 'page_blocked'
    if error.__cause__ is not None:
        return blocker_kind(error.__cause__)
    return None


class PublishIntervention:
    def __init__(self, *, headed, emit, probe, control, session_id,
                 now=time.time, sleep=asyncio.sleep):
        self.headed = headed
        self.emit = emit
        self.probe = probe
        self.control = control
        self.session_id = session_id
        self.now = now
        self.sleep = sleep
        self.deadline = None
        self.submitting = False
        self.wait_count = 0

    async def check_result(self):
        result = await self.probe()
        if result in ('published', 'scheduled'):
            self.emit({'type': 'result', 'status': result})
            raise ManualPublishCompleted()

    async def step(self, operation):
        # Each continuation retries only this operation, never the whole upload.
        for _attempt in range(100):
            if self.deadline is not None:
                await self.check_result()
                if self.now() >= self.deadline:
                    raise ManualPublishUncertain('人工处理已超时')
            try:
                return await operation()
            except Exception as error:
                kind = blocker_kind(error)
                if kind is None or self.submitting:
                    raise
                if not self.headed:
                    self.emit({'type': 'attention', 'kind': kind, 'message': str(error)[:500]})
                    raise
                await self.wait_for_user(str(error))
        raise ManualPublishUncertain('人工处理仍无法继续')

    async def wait_for_user(self, message):
        if self.wait_count:
            self.session_id = uuid.uuid4().hex
        self.wait_count += 1
        if self.deadline is None:
            self.deadline = self.now() + 600
        # Durable before announcing control. A lost click event cannot cause replay.
        self.emit({'type': 'checkpoint', 'checkpoint': 'manual_control'})
        self.emit({'type': 'intervention', 'state': 'waiting',
                   'sessionId': self.session_id, 'expiresAt': int(self.deadline * 1000),
                   'message': message[:500]})
        while self.now() < self.deadline:
            try:
                await self.check_result()
                command = await self.control()
            except Exception as error:
                raise ManualPublishUncertain(str(error)) from error
            if command and command.get('sessionId') == self.session_id:
                if command.get('action') == 'cancel':
                    raise ManualPublishUncertain('人工处理已取消')
                if command.get('action') == 'continue':
                    await self.check_result()
                    self.emit({'type': 'intervention', 'state': 'continuing',
                               'sessionId': self.session_id})
                    return
            await self.sleep(0.25)
        raise ManualPublishUncertain('人工处理已超时')
