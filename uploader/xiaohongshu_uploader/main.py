# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import inspect
import os
from datetime import datetime
from pathlib import Path

from patchright.async_api import Page
from patchright.async_api import Playwright
from patchright.async_api import async_playwright

from conf import DEBUG_MODE, LOCAL_CHROME_HEADLESS, LOCAL_CHROME_PATH
from uploader.base_video import BaseVideoUploader
from utils.base_social_media import set_init_script
from utils.login_qrcode import build_login_qrcode_path
from utils.login_qrcode import decode_qrcode_from_path
from utils.login_qrcode import print_terminal_qrcode
from utils.login_qrcode import remove_qrcode_file
from utils.login_qrcode import save_data_url_image
from utils.log import xiaohongshu_logger
from utils.popcorn_diagnostics import capture_page_diagnostic
from utils.popcorn_auth import observe_auth_page, emit_auth
from utils.popcorn_events import emit_checkpoint, emit_result

XHS_DEFAULT_CREATOR_BASE_URL = "https://creator.xiaohongshu.com"
XHS_CREATOR_BASE_URL_ENV = "SAU_XHS_CREATOR_BASE_URL"
XHS_PUBLISH_SUCCESS_URL_PATTERN = "**/publish/success?**"
XHS_LOGIN_BOX_SELECTOR = "div[class*='login-box']"
XHS_LOGIN_SWITCH_SELECTOR = "img.css-wemwzq"
XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE = "immediate"
XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED = "scheduled"


def _build_xhs_creator_url(path: str) -> str:
    base_url = os.getenv(
        XHS_CREATOR_BASE_URL_ENV,
        XHS_DEFAULT_CREATOR_BASE_URL,
    ).strip().rstrip("/")
    if not base_url:
        base_url = XHS_DEFAULT_CREATOR_BASE_URL
    return f"{base_url}/{path.lstrip('/')}"


def _msg(emoji: str, text: str) -> str:
    return f"{emoji} {text}"


async def _js_click_by_text(page: Page, text: str) -> bool:
    """用 JS 找到文字完全匹配的最内层元素并点击它及其祖先（绕过 span pointer-events:none / 遮罩拦截）。

    小红书很多可点项文字在 <span class="d-text"> 里，pointer-events 常被禁用，
    Playwright 常规 click 会超时。用原生 click 冒泡触发 Vue 事件更可靠。
    """
    return await page.evaluate(
        """(t) => {
            const nodes = [...document.querySelectorAll('*')].filter(
                e => e.children.length === 0 && (e.textContent || '').trim() === t
            );
            if (!nodes.length) return false;
            let el = nodes[nodes.length - 1];
            for (let i = 0; i < 4 && el; i++) { try { el.click(); } catch (e) {} el = el.parentElement; }
            return true;
        }""",
        text,
    )


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


async def _open_xhs_qrcode_panel(page: Page) -> None:
    login_box = page.locator(XHS_LOGIN_BOX_SELECTOR).first
    await login_box.wait_for(state="visible", timeout=30000)

    scan_text = login_box.locator("div:has-text('扫一扫')").first
    if await scan_text.count():
        return

    switch_img = login_box.locator(XHS_LOGIN_SWITCH_SELECTOR).first
    await switch_img.wait_for(state="visible", timeout=10000)
    await switch_img.click()
    await login_box.locator("div:has-text('扫一扫')").first.wait_for(state="visible", timeout=10000)


async def _find_xhs_qrcode_locator(page: Page):
    await _open_xhs_qrcode_panel(page)

    qrcode_img = page.locator('.login-box-container').get_by_text("APP扫一扫登录").filter(visible=True).locator("xpath=..//following-sibling::div//img").nth(0)

    if await qrcode_img.count():
        return qrcode_img

    raise RuntimeError("未在扫一扫登录区域找到小红书二维码图片")


async def _extract_xhs_qrcode_src(page: Page) -> str:
    qrcode_img = await _find_xhs_qrcode_locator(page)
    await qrcode_img.wait_for(state="visible", timeout=30000)
    qrcode_src = await qrcode_img.get_attribute("src")
    if not qrcode_src:
        raise RuntimeError("未获取到小红书登录二维码地址")
    return qrcode_src


async def _save_xhs_qrcode(
    page: Page,
    account_file: str,
    previous_qrcode_path: Path | None = None,
    qrcode_callback=None,
) -> dict:
    qrcode_src = await _extract_xhs_qrcode_src(page)
    qrcode_path = build_login_qrcode_path(account_file, suffix="xhs_login_qrcode")
    qrcode_img = await _find_xhs_qrcode_locator(page)

    if qrcode_src.startswith("data:image/"):
        save_data_url_image(qrcode_src, qrcode_path)
    else:
        qrcode_path.parent.mkdir(parents=True, exist_ok=True)
        await qrcode_img.screenshot(path=str(qrcode_path))

    if previous_qrcode_path and previous_qrcode_path != qrcode_path:
        if remove_qrcode_file(previous_qrcode_path):
            xiaohongshu_logger.info(_msg("🧹", f"临时二维码文件已清理: {previous_qrcode_path}"))

    xiaohongshu_logger.info(_msg("🖼️", f"二维码已经准备好啦，已保存到: {qrcode_path}"))
    qrcode_content = decode_qrcode_from_path(qrcode_path)
    if qrcode_content:
        print_terminal_qrcode(qrcode_content, qrcode_path, "小红书APP")
    else:
        xiaohongshu_logger.warning(_msg("😵", f"终端没法完整显示二维码，请打开 {qrcode_path} 扫码"))

    qrcode_info = {
        "image_path": str(qrcode_path),
        "image_data_url": qrcode_src,
    }
    await _emit_qrcode_callback(qrcode_callback, qrcode_info)
    emit_auth("qrcode_ready")
    return qrcode_info


async def _is_xhs_login_completed(page: Page) -> bool:
    if page.url.startswith(_build_xhs_creator_url("/login")):
        return False

    login_box = page.locator(XHS_LOGIN_BOX_SELECTOR).first
    if not await login_box.count():
        return True

    try:
        return not await login_box.is_visible()
    except Exception:
        return True


async def cookie_auth(account_file):
    if not os.path.exists(account_file):
        return False

    async with async_playwright() as playwright:
        if LOCAL_CHROME_PATH:
            browser = await playwright.chromium.launch(headless=True, executable_path=LOCAL_CHROME_PATH)
        else:
            browser = await playwright.chromium.launch(headless=True, channel="chromium")
        try:
            context = await browser.new_context(storage_state=account_file)
            context = await set_init_script(context)
            page = await context.new_page()
            await page.goto(
                _build_xhs_creator_url(
                    "/publish/publish?from=homepage&target=video"
                )
            )
            await page.wait_for_timeout(3000)

            if page.url.startswith(_build_xhs_creator_url("/login")):
                xiaohongshu_logger.info(_msg("🥹", "cookie 已失效，得重新登录一下"))
                return False

            login_box = page.locator(XHS_LOGIN_BOX_SELECTOR).first
            if await login_box.count():
                try:
                    if await login_box.is_visible():
                        xiaohongshu_logger.info(_msg("🥹", "页面仍然停留在登录二维码页，按 cookie 失效处理"))
                        return False
                except Exception:
                    return False

            xiaohongshu_logger.success(_msg("🥳", "cookie 有效"))
            return True
        except Exception as exc:
            xiaohongshu_logger.warning(_msg("😵", f"cookie 校验时出错，按失效处理: {exc}"))
            return False
        finally:
            await browser.close()


async def xiaohongshu_setup(
    account_file,
    handle=False,
    return_detail=False,
    qrcode_callback=None,
    headless: bool = LOCAL_CHROME_HEADLESS,
):
    if not os.path.exists(account_file) or not await cookie_auth(account_file):
        if not handle:
            result = _build_login_result(False, "cookie_invalid", "cookie文件不存在或已失效", account_file)
            return result if return_detail else False
        xiaohongshu_logger.info(_msg("🥹", "cookie 失效了，准备打开浏览器重新登录"))
        result = await xiaohongshu_cookie_gen(
            account_file,
            qrcode_callback=qrcode_callback,
            headless=headless,
        )
        return result if return_detail else result["success"]

    result = _build_login_result(True, "cookie_valid", "cookie有效", account_file)
    return result if return_detail else True


async def xiaohongshu_cookie_gen(
    account_file,
    qrcode_callback=None,
    poll_interval: int = 3,
    max_checks: int = 100,
    headless: bool = LOCAL_CHROME_HEADLESS,
):
    if headless:
        xiaohongshu_logger.info(_msg("🖼️", "小红书登录将以无头模式运行，小人会输出终端二维码并保存本地二维码图片"))

    account_path = Path(account_file)
    account_path.parent.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=headless, channel="chromium")
        context = await browser.new_context()
        context = await set_init_script(context)
        qrcode_path = None
        qrcode_info = None
        result = _build_login_result(False, "failed", "小红书登录失败", account_file)
        page = None
        try:
            page = await context.new_page()
            await page.goto(_build_xhs_creator_url("/login"))
            qrcode_info = await _save_xhs_qrcode(page, account_file, qrcode_callback=qrcode_callback)
            qrcode_path = Path(qrcode_info["image_path"])
            xiaohongshu_logger.info(_msg("🧍", "请扫码，小人正在耐心等待登录完成"))

            for _ in range(max_checks):
                if await observe_auth_page(page, "xiaohongshu"):
                    await asyncio.sleep(poll_interval)
                    continue
                if await _is_xhs_login_completed(page):
                    emit_auth("verifying")
                    await asyncio.sleep(2)
                    await context.storage_state(path=account_file)
                    if await cookie_auth(account_file):
                        xiaohongshu_logger.success(_msg("🥳", "小红书扫码登录成功，小人开心收工"))
                        result = _build_login_result(True, "success", "小红书扫码登录成功", account_file, qrcode_info, page.url)
                    else:
                        result = _build_login_result(
                            False,
                            "cookie_invalid",
                            "小红书扫码流程结束，但 cookie 校验失败",
                            account_file,
                            qrcode_info,
                            page.url,
                        )
                    return result

                await asyncio.sleep(poll_interval)

            result = _build_login_result(
                False,
                "timeout",
                "等待小红书扫码登录超时",
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
                    platform="xiaohongshu",
                    phase="auth",
                    error=result["message"],
                )
            if remove_qrcode_file(qrcode_path):
                xiaohongshu_logger.info(_msg("🧹", f"临时二维码文件已清理: {qrcode_path}"))
            if not result["success"]:
                xiaohongshu_logger.error(_msg("😢", f"登录失败: {result['message']}"))
            await context.close()
            await browser.close()
        return result


class XiaoHongShuBaseUploader(BaseVideoUploader):
    def __init__(
        self,
        publish_date: datetime | int,
        account_file,
        publish_strategy: str = XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE,
        debug: bool = DEBUG_MODE,
        headless: bool = LOCAL_CHROME_HEADLESS,
    ):
        self.publish_date = publish_date
        self.account_file = str(account_file)
        self.publish_strategy = publish_strategy
        self.debug = debug
        self.date_format = "%Y年%m月%d日 %H:%M"
        self.local_executable_path = LOCAL_CHROME_PATH
        self.headless = headless

    async def validate_base_args(self):
        if not os.path.exists(self.account_file):
            raise RuntimeError(f"cookie文件不存在，请先完成小红书登录: {self.account_file}")
        if not await cookie_auth(self.account_file):
            raise RuntimeError(f"cookie文件已失效，请先完成小红书登录: {self.account_file}")

        if self.publish_strategy not in {
            XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE,
            XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED,
        }:
            raise ValueError(f"不支持的发布策略: {self.publish_strategy}")

        if self.publish_strategy == XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED:
            self.publish_date = self.validate_publish_date(self.publish_date)
        else:
            self.publish_date = 0

    async def set_schedule_time_xiaohongshu(self, page: Page, publish_date: datetime):
        xiaohongshu_logger.info(_msg("🕒", f"小人准备设置定时发布时间: {publish_date.strftime(self.date_format)}"))
        await page.locator('.custom-switch-card').filter(has_text="定时发布").locator('.d-switch').click()
        await asyncio.sleep(1)
        publish_date_hour = publish_date.strftime("%Y-%m-%d %H:%M")
        time_input = page.locator('.d-datepicker-input-filter input.d-text')
        await time_input.fill(str(publish_date_hour))
        await asyncio.sleep(1)
        actual = (await time_input.input_value()).strip()
        if actual != publish_date_hour:
            raise RuntimeError(
                f"小红书定时发布时间写入校验失败：期望 {publish_date_hour}，实际 {actual or '空'}"
            )

    async def set_location(self, page: Page, location: str = "青岛市"):
        if not location:
            return True

        xiaohongshu_logger.info(_msg("📍", f"小人准备设置位置: {location}"))
        loc_ele = await page.wait_for_selector('div.d-text.d-select-placeholder.d-text-ellipsis.d-text-nowrap')
        await loc_ele.click()
        await page.wait_for_timeout(1000)
        await page.keyboard.type(location)
        dropdown_selector = 'div.d-popover.d-popover-default.d-dropdown.--size-min-width-large'
        await page.wait_for_timeout(2000)
        try:
            await page.wait_for_selector(dropdown_selector, timeout=3000)
        except Exception:
            xiaohongshu_logger.warning(_msg("😵", "位置下拉列表没按预期出现，小人继续按旧逻辑查找"))
        await page.wait_for_timeout(1000)
        flexible_xpath = (
            f'//div[contains(@class, "d-popover") and contains(@class, "d-dropdown")]'
            f'//div[contains(@class, "d-options-wrapper")]'
            f'//div[contains(@class, "d-grid") and contains(@class, "d-options")]'
            f'//div[contains(@class, "name") and text()="{location}"]'
        )
        await page.wait_for_timeout(3000)
        try:
            location_option = await page.wait_for_selector(
                flexible_xpath,
                timeout=3000
            )

            if not location_option:
                location_option = await page.wait_for_selector(
                    f'//div[contains(@class, "d-popover") and contains(@class, "d-dropdown")]'
                    f'//div[contains(@class, "d-options-wrapper")]'
                    f'//div[contains(@class, "d-grid") and contains(@class, "d-options")]'
                    f'/div[1]//div[contains(@class, "name") and text()="{location}"]',
                    timeout=2000
                )

            await location_option.scroll_into_view_if_needed()
            await location_option.click()
            xiaohongshu_logger.success(_msg("🥳", f"位置已经设置成 {location}"))
            return True
        except Exception as e:
            xiaohongshu_logger.error(_msg("😢", f"设置位置失败: {e}"))
            try:
                all_options = await page.query_selector_all(
                    '//div[contains(@class, "d-popover") and contains(@class, "d-dropdown")]'
                    '//div[contains(@class, "d-options-wrapper")]'
                    '//div[contains(@class, "d-grid") and contains(@class, "d-options")]'
                    '/div'
                )
                xiaohongshu_logger.debug(_msg("🧍", f"位置下拉里一共找到 {len(all_options)} 个选项"))
                for i, option in enumerate(all_options[:3]):
                    option_text = await option.inner_text()
                    xiaohongshu_logger.debug(_msg("🧾", f"候选位置 {i + 1}: {option_text.strip()[:50]}"))
            except Exception as inner_e:
                xiaohongshu_logger.debug(_msg("😵", f"读取位置候选列表失败: {inner_e}"))
            return False

    async def fill_title(self, page: Page) -> None:
        title = self.title.strip()
        if len(title) > 20:
            raise ValueError("小红书标题不能超过 20 个字符，请修改后重试")
        title_container = page.locator('input[placeholder*="填写标题"]')
        await title_container.fill(title)
        if (await title_container.input_value()).strip() != title:
            raise RuntimeError("小红书标题写入校验失败，已阻止发布")

    async def fill_desc(self, page: Page) -> None:
        if not getattr(self, "desc", ""):
            return

        # 标题为普通 input；正文是完整 contenteditable。占位 p 随换行移动，不能用作回读事实源。
        desc = page.locator('[contenteditable="true"]').first
        await desc.fill(self.desc)
        # 富文本重绘可能移动光标；先跨平台全选再折叠到末尾，避免 Enter 拆开最后一个词。
        await desc.press("ControlOrMeta+KeyA")
        await desc.press("ArrowRight")
        await desc.press("Enter")
        if " ".join(self.desc.split()) != " ".join((await desc.inner_text()).split()):
            raise RuntimeError("小红书正文描述写入校验失败，已阻止发布")

    async def fill_tags(self, page: Page) -> None:
        if not getattr(self, "tags", None):
            return

        # 小红书标签上限为 10 个，超过会导致死循环卡住发布
        max_tags = 10
        if len(self.tags) > max_tags:
            raise ValueError(
                f"小红书话题不能超过 {max_tags} 个，请修改后重试"
            )

        if not getattr(self, "desc", ""):
            desc = page.locator('[contenteditable="true"]').first
            await desc.click()

        for tag in self.tags:  # 循环处理所有 tags
            # 话题候选下拉框依赖小红书联想接口实时返回，网络抖动/无匹配时会等不到。
            # 已选择的话题必须成功写入；候选框缺失时阻止发布，不静默丢弃。
            try:
                await page.keyboard.type("#" + tag, delay=30)
                candidates = page.locator('#creator-editor-topic-container .item')
                rejection = page.get_by_text(
                    "话题内不允许包含特殊符号", exact=False
                ).first
                matched = None
                for _ in range(10):
                    if await rejection.count() and await rejection.is_visible():
                        raise RuntimeError("平台提示：话题内不允许包含特殊符号")
                    for index in range(await candidates.count()):
                        candidate = candidates.nth(index)
                        if not await candidate.is_visible():
                            continue
                        candidate_name = (await candidate.inner_text()).splitlines()[0]
                        if candidate_name.strip().lstrip("#") == tag.strip().lstrip("#"):
                            matched = candidate
                            break
                    if matched is not None:
                        break
                    await asyncio.sleep(0.4)
                if matched is None:
                    raise RuntimeError(
                        f"未找到与『{tag}』匹配的小红书话题候选"
                    )
                await matched.click()
            except Exception as exc:
                # 清掉已键入但未成词的 "#tag" 文本，避免它残留进正文
                for _ in range(len("#" + tag)):
                    await page.keyboard.press("Backspace")
                raise RuntimeError(
                    f"小红书话题『{tag}』未能写入，已阻止发布: {exc}"
                ) from exc

        desc = page.locator('[contenteditable="true"]').first
        actual = await desc.inner_text()
        expected_tags = [f"#{tag.strip().lstrip('#')}" for tag in self.tags if tag.strip()]
        if any(tag not in actual for tag in expected_tags):
            raise RuntimeError("小红书话题写入校验失败，已阻止发布")

    async def fill_meta(self, page: Page) -> None:
        await self.fill_title(page)
        await self.fill_desc(page)
        await self.fill_tags(page)

    async def check_original_declaration(self, page: Page) -> None:
        """设置「来源转载」声明，填写转载来源。

        流程（对应 codegen 录制）：
          点「添加内容类型声明」→ 点包含「来源转载」的 div
          → 填 placeholder「请输入媒体名称」→ 点 button「确认」。
        仅调用方显式提供来源时设置；任一步失败都中断当前发布目标。
        """
        source = getattr(self, "repost_source", "") or ""
        if not source.strip():
            return
        try:
            # 1. 点「添加内容类型声明」
            trigger = page.get_by_text("添加内容类型声明", exact=False).first
            try:
                await trigger.scroll_into_view_if_needed(timeout=5000)
            except Exception:
                pass
            await trigger.click(force=True)
            await page.wait_for_timeout(1500)

            # 2. 选「来源转载」选项
            import re as _re
            repost_option = page.locator("#publish-container div").filter(
                has_text=_re.compile(r"^来源转载$")
            ).last
            if await repost_option.count():
                await repost_option.click(force=True)
            else:
                await _js_click_by_text(page, "来源转载")
            await page.wait_for_timeout(1500)

            # 3. 填写媒体名称
            source_input = page.get_by_placeholder("请输入媒体名称").first
            await source_input.wait_for(state="visible", timeout=8000)
            await source_input.click()
            await source_input.fill(source)
            await page.wait_for_timeout(500)

            # 4. 点「确认」按钮
            confirm = page.get_by_role("button", name="确认").first
            try:
                await confirm.wait_for(state="visible", timeout=5000)
                await confirm.click()
            except Exception:
                await _js_click_by_text(page, "确认")

            await page.wait_for_timeout(1000)
            xiaohongshu_logger.success(_msg("🧾", f"来源转载已声明（来源：{source}）"))
        except Exception as exc:
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass
            raise RuntimeError(f"来源转载声明设置失败: {exc}") from exc


class XiaoHongShuVideo(XiaoHongShuBaseUploader):
    def __init__(
        self,
        title,
        file_path,
        tags,
        publish_date: datetime | int,
        account_file,
        thumbnail_path=None,
        desc: str | None = None,
        publish_strategy: str = XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE,
        debug: bool = DEBUG_MODE,
        headless: bool = LOCAL_CHROME_HEADLESS,
        repost_source: str | None = None,
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
        self.repost_source = repost_source.strip() if repost_source and repost_source.strip() else ""

    async def validate_upload_args(self):
        await self.validate_base_args()
        if not self.title or not str(self.title).strip():
            raise ValueError("视频模式下，title 是必须的")

        self.file_path = str(self.validate_video_file(self.file_path))
        if self.thumbnail_path:
            self.thumbnail_path = str(self.validate_image_file(self.thumbnail_path))

    async def handle_upload_error(self, page: Page):
        xiaohongshu_logger.warning(_msg("😵", "视频上传摔了一跤，小人马上重新上传"))
        await page.locator('div.progress-div [class^="upload-btn-input"]').set_input_files(self.file_path)

    async def set_thumbnail(self, page: Page, thumbnail_path: str):
        if not thumbnail_path:
            return

        xiaohongshu_logger.info(_msg("🖼️", "小人准备设置封面"))

        try:
            async def is_visible(locator) -> bool:
                if not await locator.count():
                    return False
                try:
                    return await locator.is_visible()
                except Exception:
                    return True

            async def find_modal():
                for selector in (
                    "div.d-modal.cover-modal:visible",
                    "div.d-modal:visible",
                ):
                    candidate = page.locator(selector).first
                    if await is_visible(candidate):
                        return candidate
                return None

            async def find_visible_text(scope, labels, *, exact=True):
                for label in labels:
                    matches = scope.get_by_text(label, exact=exact)
                    for index in range(await matches.count()):
                        candidate = matches.nth(index)
                        if await is_visible(candidate):
                            return candidate
                return None

            async def find_crop_control(scope):
                # 新版编辑器把点击事件绑定在 div.item；内层 span.name 仅承载
                # “裁剪”文字，直接点击它不会切换面板。
                for label in ("剪裁", "裁剪"):
                    candidate = scope.locator("div.category div.item").filter(
                        has_text=label
                    ).first
                    if await is_visible(candidate):
                        return candidate
                return await find_visible_text(scope, ("剪裁", "裁剪"))

            async def find_upload_tab(scope):
                return await find_visible_text(
                    scope,
                    ("上传封面", "上传图片", "上传封面图片"),
                )

            async def find_image_input(scope):
                for selector in (
                    'input.upload-input[type="file"][accept*="image"]',
                    'div.upload-wrapper input[type="file"][accept*="image"]',
                    'input[type="file"][accept*="image"]',
                ):
                    candidate = scope.locator(selector).first
                    if await candidate.count():
                        return candidate
                return None

            async def find_upload_controls():
                active_modal = await find_modal()
                active_editor = active_modal or page
                return (
                    active_modal,
                    await find_upload_tab(active_editor),
                    await find_image_input(active_editor),
                )

            async def wait_for_upload_controls(timeout_ms=30000):
                active_modal = None
                upload_control = None
                image_input = None
                poll_interval_ms = 500
                for attempt in range(max(1, timeout_ms // poll_interval_ms)):
                    active_modal, upload_control, image_input = await find_upload_controls()
                    if upload_control is not None or image_input is not None:
                        break
                    if attempt + 1 < timeout_ms // poll_interval_ms:
                        await page.wait_for_timeout(poll_interval_ms)
                return active_modal, upload_control, image_input

            async def wait_for_modal(timeout_ms=30000):
                poll_interval_ms = 500
                for attempt in range(max(1, timeout_ms // poll_interval_ms)):
                    active_modal = await find_modal()
                    if active_modal is not None:
                        return active_modal
                    if attempt + 1 < timeout_ms // poll_interval_ms:
                        await page.wait_for_timeout(poll_interval_ms)
                return None

            async def wait_for_visible_text(scope, labels, *, exact=True, timeout_ms=30000):
                poll_interval_ms = 500
                for attempt in range(max(1, timeout_ms // poll_interval_ms)):
                    control = await find_visible_text(scope, labels, exact=exact)
                    if control is not None:
                        return control
                    if attempt + 1 < timeout_ms // poll_interval_ms:
                        await page.wait_for_timeout(poll_interval_ms)
                return None

            async def wait_for_crop_control(scope, timeout_ms=30000):
                poll_interval_ms = 500
                for attempt in range(max(1, timeout_ms // poll_interval_ms)):
                    control = await find_crop_control(scope)
                    if control is not None:
                        return control
                    if attempt + 1 < timeout_ms // poll_interval_ms:
                        await page.wait_for_timeout(poll_interval_ms)
                return None

            async def wait_for_cover_video_ready(scope, timeout_ms=300000):
                loading_labels = ("视频加载中", "正在解析视频流")
                poll_interval_ms = 1000
                waiting_logged = False
                for attempt in range(max(1, timeout_ms // poll_interval_ms)):
                    loading = await find_visible_text(
                        scope,
                        loading_labels,
                        exact=False,
                    )
                    if loading is None:
                        return
                    if not waiting_logged:
                        xiaohongshu_logger.info(
                            _msg("⏳", "封面编辑器仍在解析视频，小人等解析完成后再裁剪")
                        )
                        waiting_logged = True
                    if attempt + 1 < timeout_ms // poll_interval_ms:
                        await page.wait_for_timeout(poll_interval_ms)
                raise RuntimeError("等待小红书封面视频解析完成超时")

            async def wait_for_image_input(fallback_editor, timeout_ms=10000):
                active_modal = await find_modal()
                active_editor = active_modal or fallback_editor
                image_input = None
                poll_interval_ms = 500
                for attempt in range(max(1, timeout_ms // poll_interval_ms)):
                    active_modal = await find_modal() or active_modal
                    active_editor = active_modal or fallback_editor
                    image_input = await find_image_input(active_editor)
                    if image_input is not None:
                        break
                    if attempt + 1 < timeout_ms // poll_interval_ms:
                        await page.wait_for_timeout(poll_interval_ms)
                return active_modal, active_editor, image_input

            cover_section = page.locator("text=设置封面").first
            try:
                await cover_section.scroll_into_view_if_needed(timeout=5000)
            except Exception:
                pass
            await page.wait_for_timeout(2000)

            modal, upload_tab, file_input = await find_upload_controls()

            if modal is None and upload_tab is None and file_input is None:
                smart_cover_entry = page.locator("div.cover-edit-entry").first
                if await is_visible(smart_cover_entry):
                    await _js_click_by_text(page, "编辑封面")
                    modal = await wait_for_modal()

            # 默认封面卡片的方向跟随视频，而不是待上传封面。横屏卡片为
            # default.row，内部隐藏蒙层仍为 default.column；只定位 cover
            # 的直接子卡片，避免命中蒙层或智能推荐封面。hover 后入口才显示。
            if modal is None and upload_tab is None and file_input is None:
                default_cover = page.locator(
                    "div.cover-plugin-preview div.cover > div.default"
                ).first
                if await is_visible(default_cover):
                    try:
                        await default_cover.hover(timeout=5000)
                    except Exception:
                        await default_cover.click(force=True)

                    edit_cover = await wait_for_visible_text(
                        page,
                        ("编辑封面",),
                        timeout_ms=3000,
                    )
                    if edit_cover is None:
                        await default_cover.click(force=True)
                        edit_cover = await wait_for_visible_text(
                            page,
                            ("编辑封面",),
                            timeout_ms=3000,
                        )
                    if edit_cover is not None:
                        await edit_cover.click(force=True)
                        modal = await wait_for_modal()

            # 兼容旧页面或已经展开的内嵌编辑器；逐个尝试可见入口，直到
            # 弹窗/上传控件真正出现，避免同名隐藏节点遮蔽当前可见入口。
            if modal is None and upload_tab is None and file_input is None:
                trigger_candidates = [
                    page.get_by_text("编辑封面", exact=True).first,
                    page.locator('button:has-text("编辑封面")').first,
                    page.locator("div.upload-cover").first,
                    page.locator("div.cover-plugin-preview").first,
                    page.locator('button:has-text("设置封面")').first,
                    page.locator('[class*="cover"]:has-text("设置封面")').first,
                ]
                for trigger in trigger_candidates:
                    if not await is_visible(trigger):
                        continue
                    await trigger.click(force=True)
                    modal = await wait_for_modal(timeout_ms=3000)
                    modal, upload_tab, file_input = await find_upload_controls()
                    if modal is not None or upload_tab is not None or file_input is not None:
                        break

            editor = modal or page

            # 新版编辑器要求明确选择「裁剪 → 3:4」。Popcorn 当前只向小红书
            # 提交竖屏封面；找不到该比例时不得回退为横屏或视频首帧。
            crop_control = await wait_for_crop_control(
                editor,
                timeout_ms=300000 if modal is not None else 1000,
            )
            if modal is not None and crop_control is None:
                raise RuntimeError("封面编辑器加载超时，未找到裁剪控件")
            if crop_control is not None:
                if modal is not None:
                    await wait_for_cover_video_ready(editor)
                    crop_control = await wait_for_crop_control(
                        editor,
                        timeout_ms=10000,
                    )
                    if crop_control is None:
                        raise RuntimeError("视频解析完成后未找到裁剪控件")
                await crop_control.click(force=True)
                editor = await find_modal() or editor
                portrait_ratio = editor.get_by_role("button", name="3:4").first
                if not await is_visible(portrait_ratio):
                    portrait_ratio = await wait_for_visible_text(
                        editor,
                        (
                            "竖屏（3:4）",
                            "竖屏(3:4)",
                            "竖屏 3:4",
                            "竖屏封面（3:4）",
                            "竖屏封面(3:4)",
                            "3:4",
                        ),
                        exact=False,
                    )
                if portrait_ratio is None:
                    raise RuntimeError("进入剪裁后未找到竖屏（3:4）比例")
                await portrait_ratio.click(force=True)
                modal, upload_tab, file_input = await wait_for_upload_controls()
                if upload_tab is None and file_input is None:
                    raise RuntimeError("封面编辑器加载超时，未找到上传封面控件")
            else:
                modal, upload_tab, file_input = await find_upload_controls()

            editor = modal or page
            if upload_tab is not None and file_input is None:
                await upload_tab.click()
                latest_modal, editor, file_input = await wait_for_image_input(editor)
                modal = latest_modal or modal
            if file_input is None:
                raise RuntimeError("未找到可用的上传封面控件")

            await file_input.set_input_files(thumbnail_path)
            await page.wait_for_timeout(4000)  # 等图片加载+裁剪渲染

            # 新版按钮文案为「完成」；「确定」仅保留给旧弹窗。
            confirm = None
            for label in ("完成", "确定"):
                candidate = editor.get_by_role("button", name=label).first
                if await is_visible(candidate):
                    confirm = candidate
                    break
            if confirm is None:
                confirm = editor.locator("button.mojito-button").filter(has_text="确定").first
            await confirm.wait_for(state="visible", timeout=10000)
            await confirm.click()

            # 弹窗关闭或内嵌确认按钮消失，是平台已应用封面的最小可观察回读。
            if modal is not None:
                await modal.wait_for(state="hidden", timeout=15000)
            else:
                await confirm.wait_for(state="hidden", timeout=15000)
            xiaohongshu_logger.success(_msg("🥳", "封面已经设置完成"))
        except Exception as exc:
            xiaohongshu_logger.error(_msg("🖼️", f"封面设置失败，已阻止发布：{exc}"))
            try:
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(500)
            except Exception:
                pass
            raise RuntimeError(f"小红书封面设置失败，已阻止发布：{exc}") from exc

    async def upload_video_content(self, page: Page) -> None:
        xiaohongshu_logger.info(_msg("🏃", f"小人开始搬运视频: {self.title}.mp4"))
        xiaohongshu_logger.info(_msg("🧭", "小人正在赶往视频发布页"))
        publish_url = _build_xhs_creator_url(
            "/publish/publish?from=homepage&target=video"
        )
        await page.goto(publish_url)
        await page.wait_for_url(publish_url)
        await page.locator("div[class^='upload-content'] input[class='upload-input']").set_input_files(self.file_path)

        for _attempt in range(300):
            state = await page.evaluate("""
                () => {
                    const visible = e => !!(e.getClientRects().length);
                    const headings = [...document.querySelectorAll('*')].filter(
                        e => visible(e) && e.children.length === 0 &&
                             e.textContent.trim() === '视频文件'
                    );
                    let region = headings[0];
                    while (region && !/取消上传|重新上传|上传成功|上传完成/.test(region.innerText || '')) {
                        region = region.parentElement;
                    }
                    // Legacy layout: inspect only its upload preview, never the title editor.
                    region = region || [...document.querySelectorAll('.preview-new')].find(visible);
                    if (!region || region === document.body || region === document.documentElement) return 'unknown';
                    const text = region.innerText || '';
                    if (/上传失败|上传出错/.test(text)) return 'failed';
                    if (/上传中|正在上传|转码中|处理中|解析中/.test(text)) return 'uploading';
                    const percentages = [...text.matchAll(/(\\d+(?:\\.\\d+)?)\\s*%/g)];
                    if (percentages.some(m => Number(m[1]) < 100)) return 'uploading';
                    if (/上传成功|上传完成|重新上传|分辨率/.test(text) ||
                        percentages.some(m => Number(m[1]) === 100)) return 'complete';
                    return 'unknown';
                }
            """)
            if state == "failed":
                raise RuntimeError("小红书视频上传失败，已停止设置封面")
            if state == "complete":
                xiaohongshu_logger.success(_msg("🥳", "视频上传完成，开始编辑发布内容"))
                break
            await asyncio.sleep(2)
        else:
            raise TimeoutError("等待小红书视频上传完成超时，未开始设置封面")

        xiaohongshu_logger.info(_msg("✍️", "小人开始填标题、描述和话题"))
        await self.fill_meta(page)

        await self.set_thumbnail(page, self.thumbnail_path)

        # await self.set_location(page, "青岛市")

        await self.check_original_declaration(page)

        if self.publish_strategy == XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED and self.publish_date != 0:
            await self.set_schedule_time_xiaohongshu(page, self.publish_date)

        submit_click_sent = False
        for _attempt in range(120):
            try:
                if not submit_click_sent:
                    emit_checkpoint("submitting")
                    if self.publish_strategy == XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED:
                        await page.locator('button:has-text("定时发布")').click()
                    else:
                        await page.locator('button:has-text("发布")').click()
                    submit_click_sent = True
                await page.wait_for_url(
                    XHS_PUBLISH_SUCCESS_URL_PATTERN,
                    timeout=3000
                )
                xiaohongshu_logger.success(_msg("🥳", "视频发布成功，小人开心收工"))
                emit_result("scheduled" if self.publish_strategy == XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED else "published")
                break
            except Exception:
                xiaohongshu_logger.info(_msg("🏃", "小人正在冲刺发布视频"))
                if self.debug:
                    await page.screenshot(full_page=True)
                await asyncio.sleep(0.5)
        else:
            raise TimeoutError("等待小红书视频发布结果超时")

    async def upload(self, playwright: Playwright) -> None:
        xiaohongshu_logger.info(_msg("🧍", "小人先检查 cookie、视频文件、封面和发布时间"))
        await self.validate_upload_args()
        xiaohongshu_logger.info(_msg("🥳", "上传前检查通过"))
        browser = await playwright.chromium.launch(headless=self.headless, channel="chromium")
        context = await browser.new_context(
            permissions=["geolocation"],
            storage_state=self.account_file,
        )
        context = await set_init_script(context)

        page = None
        try:
            page = await context.new_page()
            await self.upload_video_content(page)
            await context.storage_state(path=self.account_file)
            xiaohongshu_logger.success(_msg("🥳", "cookie 更新完毕"))
        except Exception as exc:
            if page is not None:
                await capture_page_diagnostic(
                    page,
                    platform="xiaohongshu",
                    phase="publish",
                    error=exc,
                )
            raise
        finally:
            await context.close()
            await browser.close()

    async def xiaohongshu_upload_video(self):
        async with async_playwright() as playwright:
            await self.upload(playwright)

    async def main(self):
        await self.xiaohongshu_upload_video()


class XiaoHongShuNote(XiaoHongShuBaseUploader):
    def __init__(
        self,
        image_paths,
        note,
        tags,
        publish_date: datetime | int,
        account_file,
        title: str | None = None,
        desc: str | None = None,
        publish_strategy: str = XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE,
        debug: bool = DEBUG_MODE,
        headless: bool = LOCAL_CHROME_HEADLESS,
        repost_source: str | None = None,
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
        self.tags = tags or []
        self.desc = desc if desc is not None else self.note
        self.title = title or ((self.desc or self.note)[:20] if (self.desc or self.note) else "")
        self.repost_source = repost_source.strip() if repost_source and repost_source.strip() else ""

    async def validate_upload_args(self):
        await self.validate_base_args()
        if not self.image_paths:
            raise ValueError("图文模式下，图片是必须的")
        if not self.title or not str(self.title).strip():
            raise ValueError("图文模式下，title 是必须的")

        if isinstance(self.image_paths, (str, Path)):
            self.image_paths = [self.image_paths]

        normalized_image_paths = []
        for image_path in self.image_paths:
            normalized_image_paths.append(str(self.validate_image_file(image_path)))
        self.image_paths = normalized_image_paths

    async def upload_note_content(self, page: Page) -> None:
        xiaohongshu_logger.info(_msg("🏃", f"小人开始搬运图文，共 {len(self.image_paths)} 张图片"))
        xiaohongshu_logger.info(_msg("🧭", "小人正在赶往图文发布页"))
        publish_url = _build_xhs_creator_url(
            "/publish/publish?from=homepage&target=image"
        )
        await page.goto(publish_url)
        await page.wait_for_url(publish_url)

        upload_input = page.locator('input[type="file"][accept*="image"]').first
        if not await upload_input.count():
            upload_input = page.locator("div[class^='upload-content'] input[class='upload-input']").first

        await upload_input.wait_for(state="attached", timeout=30000)
        xiaohongshu_logger.info(_msg("📤", "小人正在上传图片"))
        await upload_input.set_input_files(self.image_paths)

        for _attempt in range(300):
            try:
                title_container = page.locator('input[placeholder*="填写标题"]').first
                await title_container.wait_for(state="visible", timeout=3000)
                xiaohongshu_logger.success(_msg("🥳", "图文素材已经传完，可以开始填写内容了"))
                break
            except Exception:
                xiaohongshu_logger.debug(_msg("🧍", "图文素材还在上传，小人继续等一会"))
                await asyncio.sleep(1)
        else:
            raise TimeoutError("等待小红书图文上传完成超时")

        xiaohongshu_logger.info(_msg("✍️", "小人开始填标题、描述和话题"))
        await self.fill_meta(page)

        await self.check_original_declaration(page)

        if self.publish_strategy == XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED and self.publish_date != 0:
            await self.set_schedule_time_xiaohongshu(page, self.publish_date)

        submit_click_sent = False
        for _attempt in range(120):
            try:
                if not submit_click_sent:
                    emit_checkpoint("submitting")
                    if self.publish_strategy == XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED:
                        await page.locator('button:has-text("定时发布")').click()
                    else:
                        await page.locator('button:has-text("发布")').click()
                    submit_click_sent = True
                await page.wait_for_url(
                    XHS_PUBLISH_SUCCESS_URL_PATTERN,
                    timeout=3000
                )
                xiaohongshu_logger.success(_msg("🥳", "图文发布成功，小人开心收工"))
                emit_result("scheduled" if self.publish_strategy == XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED else "published")
                break
            except Exception:
                xiaohongshu_logger.info(_msg("🏃", "小人正在冲刺发布图文"))
                if self.debug:
                    await page.screenshot(full_page=True)
                await asyncio.sleep(0.5)
        else:
            raise TimeoutError("等待小红书图文发布结果超时")

    async def upload(self, playwright: Playwright) -> None:
        xiaohongshu_logger.info(_msg("🧍", "小人先检查 cookie、图片和发布时间"))
        await self.validate_upload_args()
        xiaohongshu_logger.info(_msg("🥳", "图文上传前检查通过"))
        browser = await playwright.chromium.launch(headless=self.headless, channel="chromium")
        context = await browser.new_context(
            permissions=["geolocation"],
            storage_state=self.account_file,
        )
        context = await set_init_script(context)

        page = None
        try:
            page = await context.new_page()
            await self.upload_note_content(page)
            await context.storage_state(path=self.account_file)
            xiaohongshu_logger.success(_msg("🥳", "cookie 更新完毕"))
        except Exception as exc:
            if page is not None:
                await capture_page_diagnostic(
                    page,
                    platform="xiaohongshu",
                    phase="publish",
                    error=exc,
                )
            raise
        finally:
            await context.close()
            await browser.close()

    async def xiaohongshu_upload_note(self):
        async with async_playwright() as playwright:
            await self.upload(playwright)

    async def main(self):
        await self.xiaohongshu_upload_note()
