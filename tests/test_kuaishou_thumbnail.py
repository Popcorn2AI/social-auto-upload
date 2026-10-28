import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

import cv2
import numpy as np

from uploader.ks_uploader.main import KSVideo, kuaishou_thumbnail_upload_file


def _png_bytes() -> bytes:
    image = np.full((16, 12, 3), (30, 120, 220), dtype=np.uint8)
    encoded, png = cv2.imencode(".png", image)
    if not encoded:
        raise RuntimeError("test fixture PNG encode failed")
    return png.tobytes()


def _build_thumbnail_flow(thumbnail: Path, fingerprints: list[str]):
    app = KSVideo(
        "标题",
        "/tmp/video.mp4",
        [],
        0,
        "/tmp/cookie.json",
        thumbnail_path=str(thumbnail),
    )

    events = []
    cover_card = MagicMock()
    cover_card.click = AsyncMock(side_effect=lambda: events.append("open_cover"))
    cover_card.evaluate = AsyncMock(side_effect=fingerprints)
    cover_sibling = MagicMock()
    cover_sibling.locator.return_value.nth.return_value = cover_card
    cover_label = MagicMock()
    cover_label.wait_for = AsyncMock()
    cover_label.locator.return_value = cover_sibling

    upload_tab = MagicMock()
    upload_tab.wait_for = AsyncMock()
    upload_tab.click = AsyncMock(side_effect=lambda: events.append("open_upload_tab"))
    file_input = MagicMock()
    file_input.wait_for = AsyncMock()
    file_input.set_input_files = AsyncMock(
        side_effect=lambda _value: events.append("select_file")
    )
    confirm_button = MagicMock()
    confirm_button.wait_for = AsyncMock()
    confirm_button.click = AsyncMock(side_effect=lambda **_kwargs: events.append("confirm"))

    modal = MagicMock()
    modal.wait_for = AsyncMock(
        side_effect=lambda **kwargs: events.append(f"modal_{kwargs['state']}")
    )
    modal.get_by_text.side_effect = lambda text, **_kwargs: {
        "上传封面": upload_tab,
    }[text]
    modal.locator.return_value = file_input
    modal.get_by_role.return_value = confirm_button

    page = MagicMock()
    page.wait_for_timeout = AsyncMock(
        side_effect=lambda _value: events.append("upload_settle")
    )
    span_locator = MagicMock()
    span_locator.filter.return_value = cover_label
    page.locator.side_effect = lambda selector: (
        span_locator if selector == "span" else modal
    )
    return app, page, cover_card, confirm_button, events


class KuaishouThumbnailTests(unittest.TestCase):
    def test_actual_upload_file_must_not_exceed_five_mib(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            thumbnail = Path(temp_dir) / "cover.jpg"
            original = b"\xff\xd8\xff" + b"x" * (5 * 1024 * 1024 - 3)
            thumbnail.write_bytes(original)
            with kuaishou_thumbnail_upload_file(thumbnail) as upload_path:
                self.assertEqual(Path(upload_path), thumbnail.resolve())
            thumbnail.write_bytes(original + b"x")
            with self.assertRaisesRegex(ValueError, "快手封面超过 5 MB"):
                with kuaishou_thumbnail_upload_file(thumbnail):
                    self.fail("超过限制不得上传")
            self.assertEqual(thumbnail.read_bytes(), original + b"x")

    def test_transcoded_upload_file_is_checked_after_conversion(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            thumbnail = Path(temp_dir) / "cover.png"
            original = _png_bytes()
            thumbnail.write_bytes(original)
            jpeg = np.zeros(5 * 1024 * 1024 + 1, dtype=np.uint8)
            with patch("cv2.imencode", return_value=(True, jpeg)):
                with self.assertRaisesRegex(ValueError, "快手封面超过 5 MB"):
                    with kuaishou_thumbnail_upload_file(thumbnail):
                        self.fail("转换结果超过限制不得上传")
            self.assertEqual(thumbnail.read_bytes(), original)

    def test_png_bytes_with_jpg_suffix_are_transcoded_to_temporary_jpeg(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            thumbnail = Path(temp_dir) / "cover-portrait.jpg"
            thumbnail.write_bytes(_png_bytes())

            with kuaishou_thumbnail_upload_file(thumbnail) as upload_path:
                upload_file = Path(upload_path)
                self.assertNotEqual(upload_file, thumbnail.resolve())
                self.assertEqual(upload_file.suffix, ".jpg")
                self.assertTrue(upload_file.read_bytes().startswith(b"\xff\xd8\xff"))
                self.assertTrue(upload_file.exists())

            self.assertFalse(upload_file.exists())
            self.assertTrue(thumbnail.read_bytes().startswith(b"\x89PNG"))

    def test_matching_jpeg_path_remains_a_direct_file_upload(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            thumbnail = Path(temp_dir) / "cover.jpg"
            thumbnail.write_bytes(b"\xff\xd8\xff" + b"image-data")

            with kuaishou_thumbnail_upload_file(thumbnail) as upload_value:
                self.assertEqual(upload_value, str(thumbnail.resolve()))

            self.assertTrue(thumbnail.exists())

    def test_cover_confirmation_waits_three_seconds_before_preview_stability_check(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            thumbnail = Path(temp_dir) / "cover.png"
            thumbnail.write_bytes(_png_bytes())
            app, page, cover_card, confirm_button, _ = _build_thumbnail_flow(
                thumbnail, ["old", "new", "new"]
            )
            steps = []
            confirm_button.click.side_effect = lambda **_: steps.append(("confirm", None))
            page.wait_for_timeout.side_effect = lambda ms: steps.append(("wait", ms))
            cover_card.evaluate.side_effect = lambda _: (steps.append(("preview", None)) or
                                                         ("old" if len([step for step in steps if step[0] == "preview"]) == 1 else "new"))
            asyncio.run(app.set_thumbnail(page))
            self.assertIn(("wait", 3000), steps)
            self.assertLess(steps.index(("confirm", None)), steps.index(("wait", 3000)))
            self.assertLess(steps.index(("wait", 3000)), len(steps) - 1)
            self.assertEqual(steps[steps.index(("wait", 3000)) + 1], ("preview", None))

    def test_thumbnail_waits_for_outer_preview_change_to_stabilize(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            thumbnail = Path(temp_dir) / "cover.png"
            thumbnail.write_bytes(_png_bytes())
            app, page, cover_card, confirm_button, events = _build_thumbnail_flow(
                thumbnail,
                ["generated-frame", "generated-frame", "uploaded-cover", "uploaded-cover"],
            )

            asyncio.run(app.set_thumbnail(page))

        self.assertEqual(
            events,
            [
                "open_cover",
                "modal_visible",
                "open_upload_tab",
                "select_file",
                "upload_settle",
                "confirm",
                "modal_hidden",
                "upload_settle",
                "upload_settle",
                "upload_settle",
            ],
        )
        confirm_button.click.assert_awaited_once_with(timeout=30000)
        self.assertEqual(cover_card.evaluate.await_count, 4)
        page.wait_for_timeout.assert_has_awaits([call(1000), call(3000), call(500), call(500)])
        page.on.assert_not_called()

    @patch(
        "uploader.ks_uploader.main.KUAISHOU_COVER_APPLY_TIMEOUT_SECONDS",
        0,
    )
    def test_thumbnail_blocks_publish_when_outer_preview_never_changes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            thumbnail = Path(temp_dir) / "cover.png"
            thumbnail.write_bytes(_png_bytes())
            app, page, cover_card, _confirm_button, _events = _build_thumbnail_flow(
                thumbnail,
                ["generated-frame", "generated-frame"],
            )

            with self.assertRaisesRegex(
                RuntimeError,
                "快手封面设置失败，已阻止发布：上传封面未在发布页生效",
            ):
                asyncio.run(app.set_thumbnail(page))

        self.assertEqual(cover_card.evaluate.await_count, 2)


if __name__ == "__main__":
    unittest.main()
