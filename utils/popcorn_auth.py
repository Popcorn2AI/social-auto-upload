"""Read visible login evidence only; browser fallback is owned by Popcorn."""
from utils.popcorn_events import _emit


def emit_auth(status: str) -> None:
    _emit({"type": "auth", "status": status})


SCANNED_LABELS = {
    "douyin": ("请在手机上进行确认", "扫码成功", "请在抖音中确认登录"),
    "xiaohongshu": ("扫码成功", "请在手机上确认登录", "请在小红书APP中确认登录"),
    "kuaishou": ("扫码成功", "请在手机上确认登录", "请在快手App中确认登录"),
    "wechat_channels": ("扫码成功", "请在微信中确认登录", "请在手机上确认登录", "已扫码"),
}
VERIFICATION_LABELS = ("身份验证", "安全验证", "请完成安全验证", "请完成身份验证", "短信验证", "人脸验证")


async def observe_auth_page(page, platform: str) -> str | None:
    # Login challenges may be cross-origin frames and need not navigate the page.
    for status, labels in (("verification_required", VERIFICATION_LABELS),
                           ("scanned", SCANNED_LABELS[platform])):
        for frame in page.frames:
            for label in labels:
                marker = frame.get_by_text(label, exact=True).first
                try:
                    if await marker.count() and await marker.is_visible():
                        emit_auth(status)
                        return status
                except Exception:
                    # Detached iframe during navigation is not login evidence.
                    continue
    return None
