import unittest
import importlib
import tempfile
from pathlib import Path
from contextlib import ExitStack
from unittest.mock import patch, AsyncMock, MagicMock

from utils.popcorn_auth import observe_auth_page


class Locator:
    def __init__(self, visible):
        self.visible = visible
        self.first = self

    async def count(self):
        return int(self.visible)

    async def is_visible(self):
        return self.visible

    async def wait_for(self, **_kwargs):
        if not self.visible:
            raise TimeoutError("locator unavailable")


class Frame:
    def __init__(self, labels):
        self.labels = labels

    def get_by_text(self, text, exact=False):
        return Locator(text in self.labels)


class Page(Frame):
    def __init__(self, labels=(), frame_labels=(), upload_available=False):
        super().__init__(labels)
        self.url = "https://creator.douyin.com/"
        self.frames = [self, Frame(frame_labels)]
        self.upload_available = upload_available

    def locator(self, _selector):
        return Locator(self.upload_available)


class AuthProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_douyin_login_rejects_cookie_when_upload_page_returns_to_login(self):
        module = importlib.import_module("uploader.douyin_uploader.main")
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            account_file = Path(directory) / "douyin_main.json"
            account_file.write_text(
                '{"cookies":[{"name":"sessionid","value":"present"}]}',
                encoding="utf-8",
            )
            page = Page(labels=["扫码登录"])
            page.goto = AsyncMock()
            page.wait_for_timeout = AsyncMock()
            context = MagicMock(
                new_page=AsyncMock(return_value=page),
                close=AsyncMock(),
                storage_state=AsyncMock(),
            )
            browser = MagicMock(new_context=AsyncMock(return_value=context), close=AsyncMock())
            playwright = MagicMock()
            playwright.chromium.launch = AsyncMock(return_value=browser)
            manager = MagicMock()
            manager.__aenter__ = AsyncMock(return_value=playwright)
            manager.__aexit__ = AsyncMock(return_value=False)
            stack.enter_context(patch.object(module, "async_playwright", return_value=manager))
            stack.enter_context(patch.object(module, "set_init_script", AsyncMock(return_value=context)))
            stack.enter_context(patch.object(
                module,
                "_save_douyin_qrcode",
                AsyncMock(return_value={"image_path": "", "image_data_url": ""}),
            ))
            stack.enter_context(patch.object(
                module,
                "_wait_for_douyin_login",
                AsyncMock(return_value=module._build_login_result(
                    True,
                    "success",
                    "抖音扫码登录成功",
                    str(account_file),
                    current_url="https://creator.douyin.com/creator-micro/home",
                )),
            ))
            stack.enter_context(patch.object(module, "capture_page_diagnostic", AsyncMock()))

            result = await module.douyin_cookie_gen(
                str(account_file),
                headless=True,
                max_checks=1,
                poll_interval=0,
            )

            self.assertFalse(result["success"])
            self.assertEqual(result["status"], "cookie_invalid")
            self.assertIn("上传页", result["message"])
            context.storage_state.assert_awaited_once()

    async def test_douyin_login_accepts_cookie_after_upload_page_is_available(self):
        module = importlib.import_module("uploader.douyin_uploader.main")
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            account_file = Path(directory) / "douyin_main.json"
            account_file.write_text(
                '{"cookies":[{"name":"sessionid","value":"present"}]}',
                encoding="utf-8",
            )
            page = Page(upload_available=True)

            async def goto(url, **_kwargs):
                page.url = url

            page.goto = AsyncMock(side_effect=goto)
            page.wait_for_timeout = AsyncMock()
            context = MagicMock(
                new_page=AsyncMock(return_value=page),
                close=AsyncMock(),
                storage_state=AsyncMock(),
            )
            browser = MagicMock(new_context=AsyncMock(return_value=context), close=AsyncMock())
            playwright = MagicMock()
            playwright.chromium.launch = AsyncMock(return_value=browser)
            manager = MagicMock()
            manager.__aenter__ = AsyncMock(return_value=playwright)
            manager.__aexit__ = AsyncMock(return_value=False)
            stack.enter_context(patch.object(module, "async_playwright", return_value=manager))
            stack.enter_context(patch.object(module, "set_init_script", AsyncMock(return_value=context)))
            stack.enter_context(patch.object(
                module,
                "_save_douyin_qrcode",
                AsyncMock(return_value={"image_path": "", "image_data_url": ""}),
            ))
            stack.enter_context(patch.object(
                module,
                "_wait_for_douyin_login",
                AsyncMock(return_value=module._build_login_result(
                    True,
                    "success",
                    "抖音扫码登录成功",
                    str(account_file),
                    current_url="https://creator.douyin.com/creator-micro/home",
                )),
            ))
            stack.enter_context(patch.object(module, "capture_page_diagnostic", AsyncMock()))

            result = await module.douyin_cookie_gen(
                str(account_file),
                headless=True,
                max_checks=1,
                poll_interval=0,
            )

            self.assertTrue(result["success"])
            self.assertEqual(result["status"], "success")
            self.assertEqual(
                page.url,
                "https://creator.douyin.com/creator-micro/content/upload",
            )

    async def test_all_login_entrypoints_observe_verification_and_close_without_saving_cookie(self):
        cases = (
            ("douyin_uploader", "douyin_cookie_gen", "_save_douyin_qrcode"),
            ("xiaohongshu_uploader", "xiaohongshu_cookie_gen", "_save_xhs_qrcode"),
            ("ks_uploader", "get_ks_cookie", "_save_ks_qrcode"),
            ("tencent_uploader", "tencent_cookie_gen", "_save_tencent_qrcode"),
        )
        for name, entry, save_qr in cases:
            for headless in (True, False):
                with self.subTest(platform=name, headless=headless), tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                    module = importlib.import_module(f"uploader.{name}.main")
                    page = Page(frame_labels=["身份验证"])
                    page.goto = AsyncMock()
                    context = MagicMock(new_page=AsyncMock(return_value=page), close=AsyncMock(), storage_state=AsyncMock())
                    browser = MagicMock(new_context=AsyncMock(return_value=context), close=AsyncMock())
                    playwright = MagicMock()
                    playwright.chromium.launch = AsyncMock(return_value=browser)
                    manager = MagicMock()
                    manager.__aenter__ = AsyncMock(return_value=playwright)
                    manager.__aexit__ = AsyncMock(return_value=False)
                    stack.enter_context(patch.object(module, "async_playwright", return_value=manager))
                    if hasattr(module, "set_init_script"):
                        stack.enter_context(patch.object(module, "set_init_script", AsyncMock(return_value=context)))
                    stack.enter_context(patch.object(module, save_qr, AsyncMock(return_value={"image_path": str(Path(directory) / "qr.png")})))
                    stack.enter_context(patch.object(module, "capture_page_diagnostic", AsyncMock()))
                    emit = stack.enter_context(patch("utils.popcorn_auth.emit_auth"))
                    result = await getattr(module, entry)(str(Path(directory) / "account.json"), headless=headless, max_checks=1, poll_interval=0)
                    self.assertFalse(result["success"])
                    self.assertEqual(result["status"], "timeout")
                    emit.assert_called_once_with("verification_required")
                    self.assertEqual(playwright.chromium.launch.call_args.kwargs["headless"], headless)
                    context.storage_state.assert_not_awaited()
                    context.close.assert_awaited_once()
                    browser.close.assert_awaited_once()

    async def test_same_url_identity_dialog_emits_verification_required_for_all_platforms(self):
        for platform in ("douyin", "xiaohongshu", "kuaishou", "wechat_channels"):
            with self.subTest(platform=platform), patch("utils.popcorn_auth.emit_auth") as emit:
                status = await observe_auth_page(Page(frame_labels=["身份验证"]), platform)
                self.assertEqual(status, "verification_required")
                emit.assert_called_once_with("verification_required")

    async def test_explicit_phone_confirmation_is_scanned_including_iframe(self):
        for platform, label in (("douyin", "请在手机上进行确认"), ("xiaohongshu", "扫码成功"),
                                ("kuaishou", "请在手机上确认登录"), ("wechat_channels", "请在微信中确认登录")):
            with self.subTest(platform=platform), patch("utils.popcorn_auth.emit_auth") as emit:
                self.assertEqual(await observe_auth_page(Page(frame_labels=[label]), platform), "scanned")
                emit.assert_called_once_with("scanned")

    async def test_login_form_or_unrecognized_page_never_implies_scanned(self):
        with patch("utils.popcorn_auth.emit_auth") as emit:
            self.assertIsNone(await observe_auth_page(Page(["扫码登录", "验证码登录", "获取验证码"]), "douyin"))
            emit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
