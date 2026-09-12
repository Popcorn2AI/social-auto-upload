# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import inspect
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from patchright.async_api import Locator
from patchright.async_api import Page
from patchright.async_api import Playwright
from patchright.async_api import async_playwright

from conf import DEBUG_MODE, LOCAL_CHROME_HEADLESS, LOCAL_CHROME_PATH
from uploader.base_video import BaseVideoUploader
from utils.base_social_media import set_init_script
from utils.files_times import get_absolute_path
from utils.login_qrcode import build_login_qrcode_path
from utils.login_qrcode import decode_qrcode_from_path
from utils.login_qrcode import print_terminal_qrcode
from utils.login_qrcode import remove_qrcode_file
from utils.login_qrcode import save_data_url_image
from utils.log import kuaishou_logger
from utils.popcorn_diagnostics import capture_page_diagnostic
from utils.popcorn_auth import observe_auth_page, emit_auth
from utils.popcorn_events import emit_checkpoint, emit_result

KUAISHOU_UPLOAD_URL = "https://cp.kuaishou.com/article/publish/video"
KUAISHOU_MANAGE_URL = "https://cp.kuaishou.com/article/manage/video?status=2&from=publish"
KUAISHOU_LOGIN_URL = "https://passport.kuaishou.com/pc/account/login/?sid=kuaishou.web.cp.api&callback=https%3A%2F%2Fcp.kuaishou.com%2Frest%2Finfra%2Fsts%3FfollowUrl%3Dhttps%253A%252F%252Fcp.kuaishou.com%252Farticle%252Fpublish%252Fvideo%26setRootDomain%3Dtrue"
KUAISHOU_UPLOAD_URL_PATTERN = "**/article/publish/video**"
KUAISHOU_MANAGE_URL_PATTERN = "**/article/manage/video?status=2&from=publish**"
KUAISHOU_COOKIE_INVALID_SELECTOR = "div.names div.container div.name:text('机构服务')"
KUAISHOU_PUBLISH_STRATEGY_IMMEDIATE = "immediate"
KUAISHOU_PUBLISH_STRATEGY_SCHEDULED = "scheduled"
KUAISHOU_UPLOAD_TIMEOUT_SECONDS = 480
KUAISHOU_COVER_APPLY_TIMEOUT_SECONDS = 15
KUAISHOU_COVER_APPLY_POLL_MS = 500

KUAISHOU_COVER_PREVIEW_FINGERPRINT_SCRIPT = """
(element) => {
  const values = [];
  const previewNodes = element.querySelectorAll(
    'img, video, canvas, [style*="background"]'
  );
  const nodes = [element, ...previewNodes];
  for (const node of nodes) {
    if (node instanceof HTMLImageElement) {
      values.push(node.currentSrc || node.src || "");
    } else if (node instanceof HTMLVideoElement) {
      values.push(node.poster || "");
    } else if (node instanceof HTMLCanvasElement) {
      try {
        values.push(node.toDataURL("image/png"));
      } catch (_) {
        values.push(`canvas:${node.width}x${node.height}`);
      }
    }

    const inlineBackground = node.style?.backgroundImage;
    const computedBackground = window.getComputedStyle(node).backgroundImage;
    values.push(inlineBackground || "", computedBackground || "");
  }

  const media = values.filter((value) => value && value !== "none");
  const content = media.length > 0 ? media.join("|") : element.innerHTML;
  let hash = 2166136261;
  for (let index = 0; index < content.length; index += 1) {
    hash ^= content.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return `${content.length}:${(hash >>> 0).toString(16)}`;
}
"""


@contextmanager
def kuaishou_thumbnail_upload_file(file_path: str | Path):
    """Yield a real JPG file accepted by Kuaishou without mutating the work asset."""
    path = Path(file_path).expanduser().resolve()
    content = path.read_bytes()
    if path.suffix.lower() == ".jpg" and content.startswith(b"\xff\xd8\xff"):
        yield str(path)
        return

    import cv2
    import numpy as np

    image = cv2.imdecode(np.frombuffer(content, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"快手封面转换失败，无法解析图片: {path}")
    encoded, jpeg = cv2.imencode(
        ".jpg",
        image,
        [int(cv2.IMWRITE_JPEG_QUALITY), 90],
    )
    if not encoded:
        raise ValueError(f"快手封面转换失败，无法生成 JPG: {path}")

    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix="popcorn-kuaishou-cover-",
            suffix=".jpg",
            delete=False,
        ) as temporary:
            temporary.write(jpeg.tobytes())
            temporary_path = Path(temporary.name)
        yield str(temporary_path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def build_kuaishou_publish_text(
    title: str,
    desc: str,
    tags: list[str],
    max_length: int = 1000,
) -> str:
    """组合快手发布文案；任何字段都不做静默截断。"""
    normalized_title = title.strip()
    normalized_desc = desc.strip()
    if normalized_desc == normalized_title:
        normalized_desc = ""
    normalized_tags = [tag.strip().lstrip("#") for tag in tags if tag.strip().lstrip("#")]
    if len(normalized_tags) > 3:
        raise ValueError("快手话题不能超过 3 个，请修改后重试")
    tag_text = " ".join(f"#{tag}" for tag in normalized_tags)
    publish_text = "\n".join(
        part for part in (normalized_title, normalized_desc, tag_text) if part
    )
    if len(publish_text) > max_length:
        raise ValueError(
            f"快手发布文案超过 {max_length} 个字符上限，请修改标题、简介或话题后重试"
        )
    return publish_text


def _msg(emoji: str, text: str) -> str:
    return f"{emoji} {text}"


async def _dump_page_debug(page, tag: str) -> str:
    """出错时保存整页截图 + 当前 HTML，返回保存目录，便于对照新 DOM 修选择器。"""
    import time
    base = Path("ks_debug")
    base.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    try:
        await page.screenshot(path=str(base / f"{tag}_{ts}.png"), full_page=True)
    except Exception:
        pass
    try:
        (base / f"{tag}_{ts}.html").write_text(await page.content(), encoding="utf-8")
    except Exception:
        pass
    return str(base.resolve())


async def _focus_desc_editor(page) -> Locator:
    """定位并聚焦快手发布页的『描述』编辑区。

    快手创作者中心 DOM 时有改版，旧的
        get_by_text("描述").locator("xpath=following-sibling::div")
    一旦结构变化就会干等 30s 超时。这里按多种策略依次尝试（都锚定在「描述」
    标签附近，避免误点到标题框），每种短超时快速失败；全部失败则保存截图/HTML
    供排查后抛出明确错误，而不是无脑超时。
    """
    label = page.get_by_text("描述")  # 默认子串匹配，"作品描述" 等也能命中
    strategies = [
        # 旧结构：『描述』相邻 div
        lambda: label.locator("xpath=following-sibling::div"),
        # 新版描述区通常是紧随其后的富文本可编辑区
        lambda: label.locator("xpath=following::div[@contenteditable='true'][1]"),
        # 同容器内的可编辑区
        lambda: label.locator("xpath=ancestor::*[1]//div[@contenteditable='true'][1]"),
        # 兜底：其后第一个任意可编辑元素
        lambda: label.locator("xpath=following::*[@contenteditable='true'][1]"),
    ]
    last_err = None
    for i, make in enumerate(strategies):
        try:
            loc = make().first
            await loc.wait_for(state="visible", timeout=8000)
            await loc.click(force=True)
            if i > 0:
                kuaishou_logger.warning(_msg(
                    "⚠️", f"描述区改用回退策略#{i}定位成功（快手可能已改版，建议核对选择器）"))
            return loc
        except Exception as e:  # noqa: BLE001
            last_err = e
    dbg = await _dump_page_debug(page, "desc_not_found")
    raise RuntimeError(
        f"未能定位快手『描述』编辑区（疑似发布页改版）。已保存截图/HTML 到 {dbg}，"
        f"请据此更新选择器。最后错误: {last_err}")


async def _click_visible_publish_confirm(page: Page) -> bool:
    """Confirm an already-open Ant Design publish dialog before touching the page behind it."""
    modal = page.locator("div.ant-modal-confirm-centered:visible").first
    if not await modal.count():
        return False

    primary_button = modal.locator("button.ant-btn-primary:visible").first
    if not await primary_button.count():
        raise RuntimeError("快手发布确认弹窗已显示，但未找到可点击的主按钮")

    await primary_button.click(timeout=8000)
    return True


async def submit_kuaishou_publish_once(page: Page) -> None:
    """只发送一次快手发布动作；提交后的不确定结果由 Popcorn 标记为待确认。"""
    confirmed = await _click_visible_publish_confirm(page)
    if not confirmed:
        publish_button = page.get_by_text("发布", exact=True)
        if await publish_button.count() == 0:
            raise RuntimeError("未找到快手发布按钮")
        await publish_button.click()

    await asyncio.sleep(1)
    await _click_visible_publish_confirm(page)
    try:
        await page.wait_for_url(KUAISHOU_MANAGE_URL_PATTERN, timeout=15000)
    except Exception as exc:
        raise RuntimeError("快手提交后未取得平台成功证据，发布结果待确认") from exc


def _print_ks_qrcode(qrcode_content: str, qrcode_path: Path) -> None:
    try:
        print_terminal_qrcode(qrcode_content, qrcode_path, "快手APP", compact=False, border=2)
    except TypeError as exc:
        if "unexpected keyword argument 'compact'" not in str(exc):
            raise
        kuaishou_logger.warning(_msg("😵", "检测到旧版二维码打印函数，小人切回兼容模式继续登录"))
        print_terminal_qrcode(qrcode_content, qrcode_path, "快手APP")


async def _emit_qrcode_callback(qrcode_callback, payload: dict):
    if not qrcode_callback:
        return

    callback_result = qrcode_callback(payload)
    if inspect.isawaitable(callback_result):
        await callback_result


def _build_login_result(
    success: bool,
    status: str,
    message: str,
    account_file: str,
    qrcode: dict | None = None,
    current_url: str = "",
) -> dict:
    return {
        "success": success,
        "status": status,
        "message": message,
        "account_file": str(account_file),
        "qrcode": qrcode,
        "current_url": current_url,
    }


async def _is_ks_cookie_invalid(page: Page, timeout: int = 5000) -> bool:
    try:
        await page.wait_for_selector(KUAISHOU_COOKIE_INVALID_SELECTOR, timeout=timeout)
        return True
    except Exception:
        return False


async def _extract_ks_qrcode_src(page: Page) -> str:
    login_form = page.locator("main#login-form").first
    await login_form.wait_for(state="visible", timeout=30000)

    qrcode_img = login_form.locator('div.qr-login img[alt="qrcode"]').first
    try:
        if not await qrcode_img.count() or not await qrcode_img.is_visible():
            platform_switch = login_form.locator("div.platform-switch").first
            await platform_switch.wait_for(state="visible", timeout=10000)
            await platform_switch.click()
            await asyncio.sleep(1)
    except Exception:
        platform_switch = login_form.locator("div.platform-switch").first
        await platform_switch.wait_for(state="visible", timeout=10000)
        await platform_switch.click()
        await asyncio.sleep(1)

    await qrcode_img.wait_for(state="visible", timeout=15000)

    qrcode_src = await qrcode_img.get_attribute("src")
    if not qrcode_src:
        raise RuntimeError("未获取到快手登录二维码地址")

    return qrcode_src


async def _save_ks_qrcode(page: Page, account_file: str, previous_qrcode_path: Path | None = None, qrcode_callback=None) -> dict:
    qrcode_src = await _extract_ks_qrcode_src(page)
    qrcode_path = save_data_url_image(qrcode_src, build_login_qrcode_path(account_file, suffix="ks_login_qrcode"))

    if previous_qrcode_path and previous_qrcode_path != qrcode_path:
        if remove_qrcode_file(previous_qrcode_path):
            kuaishou_logger.info(_msg("🧹", f"临时二维码文件已清理: {previous_qrcode_path}"))

    kuaishou_logger.info(_msg("🖼️", f"二维码已经准备好啦，已保存到: {qrcode_path}"))
    qrcode_content = decode_qrcode_from_path(qrcode_path)
    if qrcode_content:
        _print_ks_qrcode(qrcode_content, qrcode_path)
    else:
        kuaishou_logger.warning(_msg("😵", f"终端没法完整显示二维码，请打开 {qrcode_path} 扫码"))

    qrcode_info = {
        "image_path": str(qrcode_path),
        "image_data_url": qrcode_src,
    }
    await _emit_qrcode_callback(qrcode_callback, qrcode_info)
    emit_auth("qrcode_ready")
    return qrcode_info


async def _is_ks_qrcode_expired(page: Page) -> bool:
    expired_box = page.locator("div.qrcode-status.qrcode-status-timeout").first
    try:
        if not await expired_box.count():
            return False
        return await expired_box.is_visible()
    except Exception:
        return False


async def _is_ks_login_page_gone(page: Page) -> bool:
    try:
        login_form = page.locator("main#login-form").first
        if not await login_form.count():
            return True
        return not await login_form.is_visible()
    except Exception:
        return True


async def cookie_auth(account_file):
    async with async_playwright() as playwright:
        if LOCAL_CHROME_PATH:
            browser = await playwright.chromium.launch(headless=True, executable_path=LOCAL_CHROME_PATH)
        else:
            browser = await playwright.chromium.launch(headless=True, channel="chromium")
        try:
            context = await browser.new_context(storage_state=account_file)
            context = await set_init_script(context)
            page = await context.new_page()
            await page.goto(KUAISHOU_UPLOAD_URL)
            await page.wait_for_timeout(3000)

            # 检查是否被重定向到登录页
            if "passport.kuaishou.com" in page.url:
                kuaishou_logger.info(_msg("🥹", "cookie 已失效（跳到登录页）"))
                return False

            # 检查是否停留在介绍页（未登录状态显示"立即登录"按钮）
            login_btn = page.get_by_text("立即登录")
            if await login_btn.count() > 0:
                kuaishou_logger.info(_msg("🥹", "cookie 已失效（介绍页）"))
                return False

            # 正向证明：上传按钮存在 = 真正已登录
            try:
                upload_btn = page.locator("button[class^='_upload-btn']")
                await upload_btn.wait_for(state="visible", timeout=10000)
                kuaishou_logger.success(_msg("🥳", "cookie 有效"))
                return True
            except Exception:
                # 兜底：旧版检测（"机构服务"元素出现在未登录介绍页）
                if await _is_ks_cookie_invalid(page):
                    kuaishou_logger.info(_msg("🥹", "cookie 已失效（机构服务页）"))
                    return False
                # 都没命中：保守判定为失效，避免假阳性
                kuaishou_logger.warning(_msg("😵", "无法确认 cookie 有效性，按失效处理"))
                return False
        except Exception as exc:
            kuaishou_logger.warning(_msg("😵", f"cookie 校验时出错，按失效处理: {exc}"))
            return False
        finally:
            await browser.close()


async def ks_setup(account_file, handle=False, return_detail=False, qrcode_callback=None, headless: bool = LOCAL_CHROME_HEADLESS, cdp_url: str | None = None):
    account_file = get_absolute_path(account_file, "ks_uploader")
    if not os.path.exists(account_file) or not await cookie_auth(account_file):
        if not handle:
            result = _build_login_result(False, "cookie_invalid", "cookie文件不存在或已失效", account_file)
            return result if return_detail else False
        kuaishou_logger.info(_msg("🥹", "cookie 失效了，准备重新登录快手创作者平台"))
        result = await get_ks_cookie(account_file, qrcode_callback=qrcode_callback, headless=headless, cdp_url=cdp_url)
        return result if return_detail else result["success"]

    result = _build_login_result(True, "cookie_valid", "cookie有效", account_file)
    return result if return_detail else True


async def get_ks_cookie(
    account_file,
    qrcode_callback=None,
    headless: bool = LOCAL_CHROME_HEADLESS,
    poll_interval: int = 3,
    max_checks: int = 100,
    cdp_url: str | None = None,
):
    if headless:
        kuaishou_logger.info(_msg("🖼️", "快手登录将以无头模式运行，小人会输出终端二维码并保存本地二维码图片"))

    async with async_playwright() as playwright:
        if cdp_url:
            browser = await playwright.chromium.connect_over_cdp(cdp_url)
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            should_close_context = False
        else:
            if LOCAL_CHROME_PATH:
                browser = await playwright.chromium.launch(headless=headless, executable_path=LOCAL_CHROME_PATH)
            else:
                browser = await playwright.chromium.launch(headless=headless, channel="chromium")
            context = await browser.new_context()
            should_close_context = True
        context = await set_init_script(context)
        qrcode_path = None
        qrcode_info = None
        result = _build_login_result(False, "failed", "快手登录失败", account_file)
        page = None
        try:
            page = await context.new_page()
            await page.goto(KUAISHOU_LOGIN_URL)
            kuaishou_logger.info(_msg("🧍", "请在浏览器里扫码登录快手，小人正在耐心等待"))

            qrcode_info = await _save_ks_qrcode(page, account_file, qrcode_callback=qrcode_callback)
            qrcode_path = Path(qrcode_info["image_path"])

            for _ in range(max_checks):
                if await observe_auth_page(page, "kuaishou"):
                    await asyncio.sleep(poll_interval)
                    continue
                if page.url.startswith(KUAISHOU_UPLOAD_URL) or await _is_ks_login_page_gone(page):
                    emit_auth("verifying")
                    await context.storage_state(path=account_file)
                    if await cookie_auth(account_file):
                        kuaishou_logger.success(_msg("🥳", "快手扫码登录成功，小人开心收工"))
                        result = _build_login_result(True, "success", "快手扫码登录成功", account_file, qrcode_info, page.url)
                    else:
                        kuaishou_logger.error(_msg("😢", "快手扫码完成了，但 cookie 校验失败"))
                        result = _build_login_result(
                            False,
                            "cookie_invalid",
                            "快手扫码流程结束，但 cookie 校验失败",
                            account_file,
                            qrcode_info,
                            page.url,
                        )
                    return result

                if qrcode_info and await _is_ks_qrcode_expired(page):
                    kuaishou_logger.warning(_msg("😵", "二维码失效了，小人马上去刷新"))
                    refresh_button = page.locator("p.qrcode-refresh").first
                    if await refresh_button.count():
                        await refresh_button.click()
                        await asyncio.sleep(1)
                    qrcode_info = await _save_ks_qrcode(
                        page,
                        account_file,
                        qrcode_path,
                        qrcode_callback=qrcode_callback,
                    )
                    qrcode_path = Path(qrcode_info["image_path"])

                await asyncio.sleep(poll_interval)

            result = _build_login_result(
                False,
                "timeout",
                "等待快手扫码登录超时",
                account_file,
                qrcode_info,
                page.url,
            )
        except Exception as exc:
            result = _build_login_result(False, "failed", str(exc), account_file, current_url=page.url if "page" in locals() else "")
        finally:
            if not result["success"] and page is not None:
                await capture_page_diagnostic(
                    page,
                    platform="kuaishou",
                    phase="auth",
                    error=result["message"],
                )
            if remove_qrcode_file(qrcode_path):
                kuaishou_logger.info(_msg("🧹", f"临时二维码文件已清理: {qrcode_path}"))
            if not result["success"]:
                kuaishou_logger.error(_msg("😢", f"登录失败: {result['message']}"))
            if should_close_context:
                await context.close()
            await browser.close()

    return result


class KSBaseUploader(BaseVideoUploader):
    def __init__(
        self,
        publish_date: datetime | int,
        account_file,
        publish_strategy: str | None = None,
        debug: bool = DEBUG_MODE,
        headless: bool = LOCAL_CHROME_HEADLESS,
    ):
        self.publish_date = publish_date
        self.account_file = str(account_file)
        self.publish_strategy = publish_strategy
        self.debug = debug
        self.headless = headless
        self.local_executable_path = LOCAL_CHROME_PATH
        self.date_format = "%Y-%m-%d %H:%M"

    async def validate_base_args(self):
        if not os.path.exists(self.account_file):
            raise RuntimeError(f"cookie文件不存在，请先完成快手登录: {self.account_file}")
        if not await cookie_auth(self.account_file):
            raise RuntimeError(f"cookie文件已失效，请先完成快手登录: {self.account_file}")

        if self.publish_strategy is None:
            self.publish_strategy = (
                KUAISHOU_PUBLISH_STRATEGY_SCHEDULED
                if self.publish_date != 0
                else KUAISHOU_PUBLISH_STRATEGY_IMMEDIATE
            )

        if self.publish_strategy not in {
            KUAISHOU_PUBLISH_STRATEGY_IMMEDIATE,
            KUAISHOU_PUBLISH_STRATEGY_SCHEDULED,
        }:
            raise ValueError(f"不支持的发布策略: {self.publish_strategy}")

        if self.publish_strategy == KUAISHOU_PUBLISH_STRATEGY_SCHEDULED:
            self.publish_date = self.validate_publish_date(self.publish_date)
        else:
            self.publish_date = 0

    async def set_schedule_time(self, page: Page, publish_date: datetime):
        kuaishou_logger.info(_msg("🕒", "小人准备设置定时发布时间"))
        publish_date_str = publish_date.strftime("%Y-%m-%d %H:%M:%S")

        # 1. 切换到"定时发布"radio (用文本匹配更稳)
        await page.locator('label.ant-radio-wrapper').filter(has_text="定时发布").click()
        await asyncio.sleep(2)

        # 2. 点击 picker 打开下拉面板
        await page.locator('input[placeholder="选择日期时间"]').click()
        await asyncio.sleep(1)

        # 3. 用 React 兼容的方式直接设置 input 的 value
        #    (ant-design DatePicker 是 controlled component, 必须用 native setter + bubbling event)
        js_code = """
        (newValue) => {
            const input = document.querySelector('input[placeholder="选择日期时间"]');
            if (!input) return false;
            const nativeSetter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value'
            ).set;
            nativeSetter.call(input, newValue);
            input.dispatchEvent(new Event('input', { bubbles: true }));
            input.dispatchEvent(new Event('change', { bubbles: true }));
            return true;
        }
        """
        ok = await page.evaluate(js_code, publish_date_str)
        if not ok:
            kuaishou_logger.error("❌ 找不到时间选择器输入框")
            return

        await asyncio.sleep(1)
        # 4. 按 Enter 确认
        await page.keyboard.press("Enter")
        await asyncio.sleep(2)
        actual = (
            await page.locator('input[placeholder="选择日期时间"]').input_value()
        ).strip()
        if actual != publish_date_str:
            raise RuntimeError(
                f"快手定时发布时间写入校验失败：期望 {publish_date_str}，实际 {actual or '空'}"
            )
        kuaishou_logger.info(f"✅ 定时发布时间已设置为 {publish_date_str}")

    async def close_guide_overlay(self, page: Page) -> bool:
        """关闭快手创作者平台的 Joyride 引导遮罩。

        Joyride 有两个关键元素：
        1. tooltip (alertdialog) — 引导提示框，有关闭按钮
        2. spotlight (react-joyride__spotlight) — 聚光灯遮罩层，拦截点击事件
        两者可能独立存在。必须都关掉才能正常操作页面。
        """
        closed = False

        # 方式1：点击 tooltip 的关闭/跳过按钮
        joyride_tooltip = page.locator('div[id^="react-joyride-step"] div[role="alertdialog"]')
        if await joyride_tooltip.count() > 0 and await joyride_tooltip.first.is_visible():
            print("检测到 Joyride 引导遮罩，正在关闭...")
            # 尝试多种关闭按钮 selector
            close_selectors = [
                '[aria-label="Skip"], [data-action="skip"], button[title="Skip"]',
                'button:text("跳过")',
                'button:text("我知道了")',
                'button:text("关闭")',
                'button:text("下一步")',  # 有时需要多步跳过
            ]
            for sel in close_selectors:
                btn = page.locator('div[role="alertdialog"]').locator(sel)
                if await btn.count() > 0:
                    await btn.first.click(force=True)
                    await asyncio.sleep(0.5)
                    break
            closed = True

        # 方式2：直接移除 Joyride portal（兜底，确保 spotlight 不再拦截）
        joyride_portal = page.locator('div#react-joyride-portal')
        if await joyride_portal.count() > 0:
            try:
                await page.evaluate("document.getElementById('react-joyride-portal')?.remove()")
                print("✅ 已移除 Joyride portal 遮罩")
                closed = True
            except Exception:
                pass

        # 方式3：移除 spotlight 元素
        spotlight = page.locator('div.react-joyride__spotlight')
        if await spotlight.count() > 0:
            try:
                await page.evaluate("document.querySelectorAll('.react-joyride__spotlight').forEach(e => e.remove())")
                print("✅ 已移除 Joyride spotlight")
                closed = True
            except Exception:
                pass

        if not closed:
            print("未检测到 Joyride 遮罩，继续执行")
        else:
            await asyncio.sleep(0.5)

    async def apply_original_declaration(self, page: Page) -> None:
        """Apply the Kuaishou original declaration only when explicitly requested."""
        if not getattr(self, "declare_original", False):
            return

        label = page.locator('label:text-is("作者声明")').first
        if not await label.count():
            raise RuntimeError("快手原创声明设置失败：未找到作者声明入口")

        trigger = label.locator(
            "xpath=following-sibling::div[contains(@class,'ant-select')]"
        ).first
        if not await trigger.count():
            raise RuntimeError("快手原创声明设置失败：未找到作者声明选择框")
        await trigger.locator(".ant-select-selector").click(timeout=8000)
        await page.wait_for_timeout(500)

        option = page.locator("div.ant-select-item-option").filter(has_text="原创").first
        if not await option.count():
            raise RuntimeError("快手原创声明设置失败：未找到原创选项")
        await option.click(timeout=8000)
        await page.wait_for_timeout(500)
        kuaishou_logger.success(_msg("🥳", "已选择快手原创声明"))


class KSVideo(KSBaseUploader):
    def __init__(
        self,
        title,
        file_path,
        tags,
        publish_date: datetime | int,
        account_file,
        publish_strategy: str | None = None,
        debug: bool = DEBUG_MODE,
        headless: bool = LOCAL_CHROME_HEADLESS,
        thumbnail_path=None,
        desc: str | None = None,
        collection_name: str | None = None,
        declare_original: bool = False,
    ):
        super().__init__(
            publish_date=publish_date,
            account_file=account_file,
            publish_strategy=publish_strategy,
            debug=debug,
            headless=headless,
        )
        self.title = title
        self.file_path = file_path
        self.tags = tags or []
        self.thumbnail_path = thumbnail_path
        self.desc = desc or ""
        self.collection_name = collection_name
        self.declare_original = declare_original

    async def apply_collection(self, page: Page) -> None:
        """在发布表单页选择"加入合集"下拉框（Ant Design Select，label 属性=合集名）。

        锚点用 label 文字"加入合集"精确定位紧邻的 ant-select 容器，避免误选页面上
        其它下拉框（服务类型/关联热点/作者声明/添加地点，同页面还有好几个 ant-select）。
        找不到匹配名字的合集选项时按 Escape 收起下拉，保持未选状态直接发布（界面允许留空，
        不阻断主发布流程）。
        """
        if not self.collection_name:
            return
        try:
            trigger = page.locator(
                'label:text-is("加入合集")'
            ).locator("xpath=following-sibling::div[contains(@class,'ant-select')]").first
            if await trigger.count() == 0:
                kuaishou_logger.warning(_msg("😵", "未找到\"加入合集\"下拉框，跳过归集"))
                return
            await trigger.locator(".ant-select-selector").click(timeout=8000)
            await page.wait_for_timeout(800)

            option = page.locator(f'div.ant-select-item-option[label="{self.collection_name}"]')
            if await option.count() == 0:
                kuaishou_logger.warning(
                    _msg("😵", f"合集下拉框未找到「{self.collection_name}」，跳过归集，保持未选状态")
                )
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(300)
                return

            await option.first.click(timeout=8000)
            await page.wait_for_timeout(500)
            kuaishou_logger.success(_msg("🥳", f"已选择合集：{self.collection_name}"))
        except Exception as exc:
            kuaishou_logger.warning(_msg("😵", f"选择合集失败，跳过归集继续发布: {exc}"))
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass

    async def validate_upload_args(self):
        await self.validate_base_args()
        if not self.title or not str(self.title).strip():
            raise ValueError("快手视频上传时，title 是必须的")
        self.file_path = str(self.validate_video_file(self.file_path))
        if self.thumbnail_path:
            self.thumbnail_path = str(self.validate_image_file(self.thumbnail_path))

    async def handle_upload_error(self, page: Page):
        kuaishou_logger.warning(_msg("😵", "视频上传摔了一跤，小人马上重新上传"))
        await page.locator('div.progress-div [class^="upload-btn-input"]').set_input_files(self.file_path)

    async def _wait_for_thumbnail_applied(
        self,
        page: Page,
        cover_card: Locator,
        previous_fingerprint: str,
    ) -> None:
        """Wait until Kuaishou's outer publish form has committed the uploaded cover."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + KUAISHOU_COVER_APPLY_TIMEOUT_SECONDS
        last_fingerprint = None
        stable_reads = 0

        while True:
            current_fingerprint = await cover_card.evaluate(
                KUAISHOU_COVER_PREVIEW_FINGERPRINT_SCRIPT
            )
            if current_fingerprint and current_fingerprint != previous_fingerprint:
                if current_fingerprint == last_fingerprint:
                    stable_reads += 1
                else:
                    stable_reads = 1
                if stable_reads >= 2:
                    return
            else:
                stable_reads = 0

            if loop.time() >= deadline:
                raise RuntimeError(
                    "快手封面设置失败，已阻止发布：上传封面未在发布页生效"
                )

            last_fingerprint = current_fingerprint
            await page.wait_for_timeout(KUAISHOU_COVER_APPLY_POLL_MS)

    async def set_thumbnail(self, page: Page):
        if not self.thumbnail_path:
            return

        kuaishou_logger.info(_msg("🖼️", "小人准备设置封面"))

        cover_label = page.locator("span").filter(has_text="封面设置")
        await cover_label.wait_for(state="visible", timeout=30000)
        cover_card = (
            cover_label.locator("xpath=../following-sibling::div[1]")
            .locator("div")
            .nth(0)
        )
        previous_fingerprint = await cover_card.evaluate(
            KUAISHOU_COVER_PREVIEW_FINGERPRINT_SCRIPT
        )
        await cover_card.click()

        modal = page.locator('div[role="document"].ant-modal')
        await modal.wait_for(state="visible", timeout=30000)

        upload_cover_tab = modal.get_by_text("上传封面", exact=True)
        await upload_cover_tab.wait_for(state="visible", timeout=10000)
        await upload_cover_tab.click()

        file_input = modal.locator('input[type="file"]')
        await file_input.wait_for(state="attached", timeout=30000)
        with kuaishou_thumbnail_upload_file(self.thumbnail_path) as upload_path:
            await file_input.set_input_files(upload_path)
            await page.wait_for_timeout(1000)

            confirm_button = modal.get_by_role("button", name="确认", exact=True)
            await confirm_button.wait_for(state="visible", timeout=10000)
            await confirm_button.click(timeout=30000)
            await modal.wait_for(state="hidden", timeout=30000)

        await self._wait_for_thumbnail_applied(page, cover_card, previous_fingerprint)
        kuaishou_logger.success(_msg("🥳", "封面已在发布页生效"))

    async def upload(self, playwright: Playwright) -> None:
        kuaishou_logger.info(_msg("🧍", "小人先检查 cookie、视频文件、封面和发布时间"))
        await self.validate_upload_args()
        kuaishou_logger.info(_msg("🥳", "上传前检查通过"))

        if self.local_executable_path:
            browser = await playwright.chromium.launch(
                headless=self.headless,
                executable_path=self.local_executable_path,
            )
        else:
            browser = await playwright.chromium.launch(
                headless=self.headless,
                channel="chromium",
            )
        context = await browser.new_context(storage_state=self.account_file)
        context = await set_init_script(context)

        upload_success = False
        page = None
        try:
            page = await context.new_page()
            await page.goto(KUAISHOU_UPLOAD_URL)
            kuaishou_logger.info(_msg("🏃", f"小人开始搬运视频: {self.title}.mp4"))
            kuaishou_logger.info(_msg("🧭", "小人正在赶往快手上传主页"))
            await page.wait_for_url(KUAISHOU_UPLOAD_URL_PATTERN)

            upload_button = page.locator("button[class^='_upload-btn']")
            await upload_button.wait_for(state="visible", timeout=10000)

            async with page.expect_file_chooser() as fc_info:
                await upload_button.click()
            file_chooser = await fc_info.value
            await file_chooser.set_files(self.file_path)

            await asyncio.sleep(2)

            know_button = page.locator('button[type="button"] span:text("我知道了")').first
            try:
                if await know_button.count() and await know_button.is_visible():
                    await know_button.click()
            except Exception:
                pass

            await self.close_guide_overlay(page)

            kuaishou_logger.info(_msg("✍️", "小人开始填描述和话题"))
            # 再次检查并关闭 Joyride（可能在文件上传后才弹出）
            await self.close_guide_overlay(page)
            desc_editor = await _focus_desc_editor(page)
            await page.keyboard.press("Backspace")
            await page.keyboard.press("Control+KeyA")
            await page.keyboard.press("Delete")
            publish_text = build_kuaishou_publish_text(self.title, self.desc, self.tags)
            await page.keyboard.type(publish_text)
            actual_publish_text = await desc_editor.inner_text(timeout=8000)
            expected_fragments = [
                self.title.strip(),
                self.desc.strip(),
                *(f"#{tag.strip().lstrip('#')}" for tag in self.tags if tag.strip()),
            ]
            if any(fragment and fragment not in actual_publish_text for fragment in expected_fragments):
                raise RuntimeError("快手标题、简介或话题写入校验失败，已阻止发布")

            loop = asyncio.get_running_loop()
            upload_deadline = loop.time() + KUAISHOU_UPLOAD_TIMEOUT_SECONDS
            retry_count = 0
            while loop.time() < upload_deadline:
                try:
                    number = await page.locator("text=上传中").count()
                    if number == 0:
                        kuaishou_logger.success(_msg("🥳", "视频已经传完啦"))
                        break

                    if retry_count % 5 == 0:
                        kuaishou_logger.info(_msg("🏃", "小人正在努力上传视频"))

                    if await page.locator("text=上传失败").count():
                        await self.handle_upload_error(page)

                    await asyncio.sleep(2)
                except Exception as exc:
                    kuaishou_logger.warning(_msg("😵", f"检查上传状态时出错，小人继续重试: {exc}"))
                    await asyncio.sleep(2)
                retry_count += 1
            else:
                raise TimeoutError(
                    f"等待快手视频上传完成超时（>{KUAISHOU_UPLOAD_TIMEOUT_SECONDS}秒），已停止发布"
                )

            await self.set_thumbnail(page)

            await self.apply_collection(page)
            await self.apply_original_declaration(page)

            if self.publish_strategy == KUAISHOU_PUBLISH_STRATEGY_SCHEDULED and self.publish_date != 0:
                await self.set_schedule_time(page, self.publish_date)

            emit_checkpoint("submitting")
            await submit_kuaishou_publish_once(page)
            kuaishou_logger.success(_msg("🥳", "视频发布成功，小人开心收工"))
            emit_result("scheduled" if self.publish_strategy == KUAISHOU_PUBLISH_STRATEGY_SCHEDULED else "published")

            upload_success = True
        except Exception as exc:
            if page is not None:
                await capture_page_diagnostic(
                    page,
                    platform="kuaishou",
                    phase="publish",
                    error=exc,
                )
            raise
        finally:
            if upload_success:
                await context.storage_state(path=self.account_file)
                kuaishou_logger.success(_msg("🥳", "cookie 更新完毕"))
                await asyncio.sleep(2)
            await context.close()
            await browser.close()

    async def main(self):
        async with async_playwright() as playwright:
            await self.upload(playwright)


class KSNote(KSBaseUploader):
    def __init__(
        self,
        image_paths,
        note,
        tags,
        publish_date: datetime | int,
        account_file,
        title: str | None = None,
        publish_strategy: str | None = None,
        debug: bool = DEBUG_MODE,
        headless: bool = LOCAL_CHROME_HEADLESS,
        declare_original: bool = False,
    ):
        super().__init__(
            publish_date=publish_date,
            account_file=account_file,
            publish_strategy=publish_strategy,
            debug=debug,
            headless=headless,
        )
        self.image_paths = image_paths
        self.note = note or ""
        self.title = title or (self.note[:20] if self.note else "")
        self.tags = tags or []
        self.declare_original = declare_original

    async def validate_upload_args(self):
        await self.validate_base_args()
        if not self.title or not str(self.title).strip():
            raise ValueError("快手图文上传时，title 是必须的")
        if not self.image_paths:
            raise ValueError("快手图文上传时，图片是必须的")

        if isinstance(self.image_paths, (str, Path)):
            self.image_paths = [self.image_paths]

        normalized_image_paths = []
        for image_path in self.image_paths:
            normalized_image_paths.append(str(self.validate_image_file(image_path)))
        self.image_paths = normalized_image_paths

    async def upload_note_content(self, page: Page) -> None:
        kuaishou_logger.info(_msg("🏃", f"小人开始搬运图文，共 {len(self.image_paths)} 张图片"))
        kuaishou_logger.info(_msg("🔀", "小人正在切换到图文发布"))
        await page.locator('div[role="tablist"] div[role="tab"]:has-text("图文")').click()
        await page.wait_for_timeout(1000)

        kuaishou_logger.info(_msg("📤", "小人正在上传图片"))
        upload_button = page.locator("button[class^='_upload-btn']").filter(has_text="上传图片")
        await upload_button.wait_for(state="visible", timeout=10000)

        async with page.expect_file_chooser() as fc_info:
            await upload_button.click()
        file_chooser = await fc_info.value
        await file_chooser.set_files(self.image_paths)

        know_button = page.locator('button[type="button"] span:text("我知道了")').first
        try:
            if await know_button.count() and await know_button.is_visible():
                await know_button.click()
        except Exception:
            pass

        await self.close_guide_overlay(page)

        kuaishou_logger.info(_msg("✍️", "小人开始填写图文内容和话题"))
        desc_editor = await _focus_desc_editor(page)
        await page.keyboard.press("Backspace")
        await page.keyboard.press("Control+KeyA")
        await page.keyboard.press("Delete")
        publish_text = build_kuaishou_publish_text(self.title, self.note, self.tags)
        await page.keyboard.type(publish_text)
        actual_publish_text = await desc_editor.inner_text(timeout=8000)
        expected_fragments = [
            self.title.strip(),
            self.note.strip(),
            *(f"#{tag.strip().lstrip('#')}" for tag in self.tags if tag.strip()),
        ]
        if any(fragment and fragment not in actual_publish_text for fragment in expected_fragments):
            raise RuntimeError("快手图文标题、正文或话题写入校验失败，已阻止发布")

        max_retries = 60
        for retry_count in range(max_retries):
            try:
                number = await page.locator("text=上传中").count()
                if number == 0:
                    kuaishou_logger.success(_msg("🥳", "图文素材已经传完啦"))
                    break

                if retry_count % 5 == 0:
                    kuaishou_logger.info(_msg("🏃", "小人正在努力上传图文素材"))

                if await page.locator("text=上传失败").count():
                    kuaishou_logger.warning(_msg("😵", "图文素材上传摔了一跤，小人马上重新上传"))
                    await page.locator('div.progress-div [class^="upload-btn-input"]').set_input_files(self.image_paths)

                await asyncio.sleep(2)
            except Exception as exc:
                kuaishou_logger.warning(_msg("😵", f"检查图文上传状态时出错，小人继续重试: {exc}"))
                await asyncio.sleep(2)
        else:
            raise TimeoutError("等待快手图文素材上传完成超时，已停止发布")

        await self.apply_original_declaration(page)

        if self.publish_strategy == KUAISHOU_PUBLISH_STRATEGY_SCHEDULED and self.publish_date != 0:
            await self.set_schedule_time(page, self.publish_date)

        emit_checkpoint("submitting")
        await submit_kuaishou_publish_once(page)
        kuaishou_logger.success(_msg("🥳", "图文发布成功，小人开心收工"))
        emit_result("scheduled" if self.publish_strategy == KUAISHOU_PUBLISH_STRATEGY_SCHEDULED else "published")

    async def upload(self, playwright: Playwright) -> None:
        kuaishou_logger.info(_msg("🧍", "小人先检查 cookie、图片和发布时间"))
        await self.validate_upload_args()
        kuaishou_logger.info(_msg("🥳", "图文上传前检查通过"))

        if self.local_executable_path:
            browser = await playwright.chromium.launch(
                headless=self.headless,
                executable_path=self.local_executable_path,
            )
        else:
            browser = await playwright.chromium.launch(
                headless=self.headless,
                channel="chromium",
            )
        context = await browser.new_context(storage_state=self.account_file)
        context = await set_init_script(context)

        upload_success = False
        page = None
        try:
            page = await context.new_page()
            await page.goto(KUAISHOU_UPLOAD_URL)
            kuaishou_logger.info(_msg("🧭", "小人正在赶往快手图文发布页"))
            await page.wait_for_url(KUAISHOU_UPLOAD_URL_PATTERN)

            await self.upload_note_content(page)
            upload_success = True
        except Exception as exc:
            if page is not None:
                await capture_page_diagnostic(
                    page,
                    platform="kuaishou",
                    phase="publish",
                    error=exc,
                )
            raise
        finally:
            if upload_success:
                await context.storage_state(path=self.account_file)
                kuaishou_logger.success(_msg("🥳", "cookie 更新完毕"))
                await asyncio.sleep(2)
            await context.close()
            await browser.close()

    async def main(self):
        async with async_playwright() as playwright:
            await self.upload(playwright)
