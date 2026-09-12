import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


def _unbounded_loops(file_path: Path, method_names: set[str]) -> list[str]:
    tree = ast.parse(file_path.read_text(encoding="utf-8"))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name in method_names:
            if any(isinstance(child, ast.While) for child in ast.walk(node)):
                offenders.append(f"{node.name}:{node.lineno}")
    return offenders


class BoundedPublishTest(unittest.TestCase):
    def test_runtime_sources_only_use_declared_patchright_browser_dependency(self):
        runtime_sources = [
            *sorted((ROOT / "uploader").rglob("*.py")),
            *sorted((ROOT / "myUtils").rglob("*.py")),
        ]
        offenders = []
        for file_path in runtime_sources:
            tree = ast.parse(file_path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("playwright"):
                    offenders.append(f"{file_path.relative_to(ROOT)}:{node.lineno}")

        self.assertEqual(offenders, [])

    def test_popcorn_supported_publish_flows_have_no_unbounded_polling_loops(self):
        cases = [
            (
                ROOT / "uploader" / "douyin_uploader" / "main.py",
                {"upload", "upload_note_content"},
            ),
            (
                ROOT / "uploader" / "xiaohongshu_uploader" / "main.py",
                {"upload_video_content", "upload_note_content"},
            ),
            (
                ROOT / "uploader" / "ks_uploader" / "main.py",
                {"upload_note_content"},
            ),
        ]

        offenders = [
            f"{file_path.name}:{offender}"
            for file_path, method_names in cases
            for offender in _unbounded_loops(file_path, method_names)
        ]
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
