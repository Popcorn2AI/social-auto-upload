from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b([a-z0-9_-]*(?:cookie|token|authorization|password)[a-z0-9_-]*)\b"
    r"\s*[:=]\s*(?:bearer\s+)?[^\s,;&]+"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[^\s,;&]+")
_SAFE_COMPONENT_RE = re.compile(r"[^a-zA-Z0-9._-]+")
_DEFAULT_LIMIT = 5
_MAX_ERROR_LENGTH = 1000


def _redact(value: object) -> str:
    text = _ANSI_ESCAPE_RE.sub("", str(value))
    text = _SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
    text = _BEARER_RE.sub("Bearer [REDACTED]", text)
    return text[:_MAX_ERROR_LENGTH]


def _safe_component(value: str, fallback: str) -> str:
    safe = _SAFE_COMPONENT_RE.sub("-", value.strip()).strip("-._")
    return safe[:80] or fallback


def _diagnostic_limit() -> int:
    try:
        return max(1, int(os.environ.get("POPCORN_SAU_DIAGNOSTICS_LIMIT", _DEFAULT_LIMIT)))
    except (TypeError, ValueError):
        return _DEFAULT_LIMIT


def _prune_old_groups(directory: Path, limit: int) -> None:
    metadata_files = sorted(directory.glob("*.json"), key=lambda path: path.name, reverse=True)
    for metadata_path in metadata_files[limit:]:
        screenshot_path = metadata_path.with_suffix(".png")
        metadata_path.unlink(missing_ok=True)
        screenshot_path.unlink(missing_ok=True)


async def capture_page_diagnostic(
    page: Any,
    *,
    platform: str,
    phase: str,
    error: object,
    now: datetime | None = None,
) -> dict[str, str] | None:
    """Best-effort browser evidence capture for Popcorn-owned SAU processes."""
    directory_value = os.environ.get("POPCORN_SAU_DIAGNOSTICS_DIR", "").strip()
    if not directory_value:
        return None

    try:
        directory = Path(directory_value)
        directory.mkdir(parents=True, exist_ok=True)
        captured_at = now or datetime.now(timezone.utc)
        if captured_at.tzinfo is None:
            captured_at = captured_at.replace(tzinfo=timezone.utc)
        captured_at = captured_at.astimezone(timezone.utc)

        timestamp = captured_at.strftime("%Y%m%dT%H%M%S.%fZ")
        safe_platform = _safe_component(platform, "unknown-platform")
        safe_phase = _safe_component(phase, "unknown-phase")
        stem = f"{timestamp}-{safe_platform}-{safe_phase}"
        screenshot_path = directory / f"{stem}.png"
        metadata_path = directory / f"{stem}.json"
        temporary_metadata_path = directory / f".{stem}.json.tmp"

        await page.screenshot(path=str(screenshot_path), full_page=True)
        metadata = {
            "capturedAt": captured_at.isoformat().replace("+00:00", "Z"),
            "platform": platform,
            "phase": phase,
            "url": _redact(getattr(page, "url", "")),
            "error": _redact(error),
        }
        temporary_metadata_path.write_text(
            json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary_metadata_path.replace(metadata_path)
        _prune_old_groups(directory, _diagnostic_limit())
        return {
            "screenshot": str(screenshot_path),
            "metadata": str(metadata_path),
        }
    except Exception:
        return None
