"""Tests for CPython C extension discovery."""

import sys
import unittest
from pathlib import Path

TOOL_ROOT = Path(__file__).resolve().parents[1]
if str(TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOL_ROOT))

from refcount.c_extension import discover
from helpers import (
    MINIMAL_EXTENSION,
    SETUP_PY_TEMPLATE,
    TempSourceTree,
)


class TestCExtensionDiscovery(unittest.TestCase):
    """Test heuristic discovery of C extension project layouts."""

    def test_detect_setup_py(self):
        setup_py = SETUP_PY_TEMPLATE.format(
            name="myext",
            sources='["src/myext.c"]',
            python_requires=">=3.9",
        )
        with TempSourceTree(
            {"src/myext.c": MINIMAL_EXTENSION, "setup.py": setup_py}
        ) as root:
            result = discover(root)
            self.assertGreaterEqual(len(result["extensions"]), 1)
            ext = result["extensions"][0]
            self.assertEqual(ext["module_name"], "myext")
            self.assertEqual(ext["detection_method"], "setup_py")
            self.assertIn("src/myext.c", ext["source_files"])

    def test_detect_python_h_include(self):
        with TempSourceTree({"myext.c": MINIMAL_EXTENSION}) as root:
            result = discover(root)
            self.assertGreaterEqual(len(result["extensions"]), 1)
            self.assertEqual(
                result["extensions"][0]["detection_method"],
                "python_h_include",
            )

    def test_detect_limited_api(self):
        code = "#define Py_LIMITED_API 0x030A0000\n" + MINIMAL_EXTENSION
        with TempSourceTree({"myext.c": code}) as root:
            result = discover(root)
            self.assertTrue(result["limited_api"])
            self.assertEqual(result["limited_api_version"], "0x030A0000")

    def test_no_extension_found(self):
        with TempSourceTree({"readme.txt": "just text"}) as root:
            result = discover(root)
            self.assertEqual(result["extensions"], [])

    def test_type_stubs_detected(self):
        with TempSourceTree(
            {
                "myext.c": "#include <Python.h>\n",
                "myext.pyi": "def foo() -> int: ...\n",
                "build/generated.pyi": "def skipped() -> int: ...\n",
            }
        ) as root:
            result = discover(root)
            self.assertEqual(result["type_stubs"], ["myext.pyi"])


if __name__ == "__main__":
    unittest.main()
