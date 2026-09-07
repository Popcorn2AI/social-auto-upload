import json
import os

EVENT_PREFIX = "SAU_EVENT "


def _emit(payload: dict) -> None:
    line = EVENT_PREFIX + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    event_file = os.environ.get("POPCORN_SAU_EVENT_FILE", "").strip()
    if event_file:
        os.makedirs(os.path.dirname(event_file), exist_ok=True)
        with open(event_file, "a", encoding="utf-8") as output:
            output.write(line + "\n")
            output.flush()
            os.fsync(output.fileno())
    print(line, flush=True)


def emit_checkpoint(checkpoint: str) -> None:
    _emit({"type": "checkpoint", "checkpoint": checkpoint})


def emit_result(status: str, platform_work_id: str | None = None) -> None:
    payload = {"type": "result", "status": status}
    if platform_work_id:
        payload["platformWorkId"] = platform_work_id
    _emit(payload)
