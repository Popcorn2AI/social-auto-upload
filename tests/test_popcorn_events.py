import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout

from utils.popcorn_events import emit_checkpoint, emit_result


class PopcornEventsTests(unittest.TestCase):
    def test_events_are_single_line_machine_readable_json(self):
        output = io.StringIO()
        with redirect_stdout(output):
            emit_checkpoint("submitting")
            emit_result("scheduled", platform_work_id="123")

        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        prefix = "SAU_EVENT "
        self.assertTrue(all(line.startswith(prefix) for line in lines))
        self.assertEqual(json.loads(lines[0][len(prefix):]), {
            "type": "checkpoint",
            "checkpoint": "submitting",
        })
        self.assertEqual(json.loads(lines[1][len(prefix):]), {
            "type": "result",
            "status": "scheduled",
            "platformWorkId": "123",
        })

    def test_events_are_fsynced_to_the_popcorn_event_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            event_path = os.path.join(temp_dir, "target.events")
            previous = os.environ.get("POPCORN_SAU_EVENT_FILE")
            os.environ["POPCORN_SAU_EVENT_FILE"] = event_path
            try:
                with redirect_stdout(io.StringIO()):
                    emit_checkpoint("submitting")
                with open(event_path, encoding="utf-8") as event_file:
                    self.assertEqual(
                        event_file.read(),
                        'SAU_EVENT {"type":"checkpoint","checkpoint":"submitting"}\n',
                    )
            finally:
                if previous is None:
                    os.environ.pop("POPCORN_SAU_EVENT_FILE", None)
                else:
                    os.environ["POPCORN_SAU_EVENT_FILE"] = previous


if __name__ == "__main__":
    unittest.main()
