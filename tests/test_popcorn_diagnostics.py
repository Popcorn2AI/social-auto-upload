import json
import inspect
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from utils.popcorn_diagnostics import capture_page_diagnostic
from uploader.douyin_uploader.main import DouYinNote, DouYinVideo, douyin_cookie_gen
from uploader.ks_uploader.main import KSNote, KSVideo, get_ks_cookie
from uploader.tencent_uploader.main import TencentNote, TencentVideo, tencent_cookie_gen
from uploader.xiaohongshu_uploader.main import (
    XiaoHongShuNote,
    XiaoHongShuVideo,
    xiaohongshu_cookie_gen,
)


class FakePage:
    def __init__(self, url: str = "https://creator.example/publish"):
        self.url = url
        self.screenshot_calls = []

    async def screenshot(self, *, path: str, full_page: bool):
        self.screenshot_calls.append({"path": path, "full_page": full_page})
        Path(path).write_bytes(b"png")


class PopcornDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    def test_all_supported_publish_and_qrcode_flows_capture_failures(self):
        publish_flows = (
            DouYinVideo.douyin_upload_video,
            DouYinNote.upload,
            KSVideo.upload,
            KSNote.upload,
            TencentVideo.upload,
            TencentNote.upload,
            XiaoHongShuVideo.upload,
            XiaoHongShuNote.upload,
        )
        qrcode_flows = (
            douyin_cookie_gen,
            get_ks_cookie,
            tencent_cookie_gen,
            xiaohongshu_cookie_gen,
        )

        for flow in publish_flows:
            with self.subTest(flow=flow.__qualname__):
                source = inspect.getsource(flow)
                self.assertIn("capture_page_diagnostic", source)
                self.assertIn('phase="publish"', source)

        for flow in qrcode_flows:
            with self.subTest(flow=flow.__qualname__):
                source = inspect.getsource(flow)
                self.assertIn("capture_page_diagnostic", source)
                self.assertIn('phase="auth"', source)

    async def test_capture_keeps_latest_five_png_json_groups_and_redacts_secrets(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            previous = {
                name: os.environ.get(name)
                for name in (
                    "POPCORN_SAU_DIAGNOSTICS_DIR",
                    "POPCORN_SAU_DIAGNOSTICS_KIND",
                    "POPCORN_SAU_DIAGNOSTICS_LIMIT",
                )
            }
            os.environ["POPCORN_SAU_DIAGNOSTICS_DIR"] = temp_dir
            os.environ["POPCORN_SAU_DIAGNOSTICS_KIND"] = "publish"
            os.environ["POPCORN_SAU_DIAGNOSTICS_LIMIT"] = "5"
            try:
                page = FakePage("https://creator.example/publish?access_token=url-secret&foo=ok")
                started_at = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)
                for offset in range(6):
                    await capture_page_diagnostic(
                        page,
                        platform="xiaohongshu",
                        phase=f"publish-{offset}",
                        error="\x1b[31mTimeout cookie=session-secret token: bearer-secret\x1b[0m",
                        now=started_at + timedelta(seconds=offset),
                    )

                png_files = sorted(Path(temp_dir).glob("*.png"))
                json_files = sorted(Path(temp_dir).glob("*.json"))
                self.assertEqual(len(png_files), 5)
                self.assertEqual(len(json_files), 5)
                self.assertNotIn("publish-0", [json.loads(path.read_text())["phase"] for path in json_files])

                latest = json.loads(json_files[-1].read_text(encoding="utf-8"))
                self.assertEqual(
                    set(latest),
                    {"capturedAt", "platform", "phase", "url", "error"},
                )
                self.assertEqual(latest["platform"], "xiaohongshu")
                self.assertEqual(latest["phase"], "publish-5")
                self.assertIn("creator.example/publish", latest["url"])
                self.assertIn("foo=ok", latest["url"])
                self.assertNotIn("url-secret", latest["url"])
                self.assertNotIn("session-secret", latest["error"])
                self.assertNotIn("bearer-secret", latest["error"])
                self.assertNotIn("\x1b", latest["error"])
                self.assertTrue(all(call["full_page"] for call in page.screenshot_calls))
            finally:
                for name, value in previous.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value

    async def test_capture_is_disabled_without_a_diagnostics_directory(self):
        previous = os.environ.pop("POPCORN_SAU_DIAGNOSTICS_DIR", None)
        try:
            page = FakePage()
            result = await capture_page_diagnostic(
                page,
                platform="douyin",
                phase="publish",
                error=RuntimeError("failed"),
            )
            self.assertIsNone(result)
            self.assertEqual(page.screenshot_calls, [])
        finally:
            if previous is not None:
                os.environ["POPCORN_SAU_DIAGNOSTICS_DIR"] = previous


if __name__ == "__main__":
    unittest.main()
