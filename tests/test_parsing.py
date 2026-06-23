"""Tests for Tree-sitter function extraction helpers."""

import sys
import unittest
from pathlib import Path

TOOL_ROOT = Path(__file__).resolve().parents[1]
if str(TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOL_ROOT))

try:
    import tree_sitter  # noqa: F401
    import tree_sitter_c  # noqa: F401
except ImportError:
    HAS_TREE_SITTER = False
else:
    HAS_TREE_SITTER = True

if HAS_TREE_SITTER:
    from analysis.parsing import (
        extract_functions,
        is_cpp_available,
        parse_bytes_for_file,
        parse_string,
    )
else:
    extract_functions = None
    is_cpp_available = None
    parse_bytes_for_file = None
    parse_string = None


@unittest.skipUnless(
    HAS_TREE_SITTER,
    "tree-sitter and tree-sitter-c are required for parsing tests",
)
class TestFunctionExtraction(unittest.TestCase):
    """Test C and C++ function extraction boundaries."""

    def test_c_struct_function_pointer_is_not_function(self):
        source = b"""\
struct Handler {
    int (*callback)(int);
};

int real_function(void)
{
    return 1;
}
"""
        tree = parse_string(source.decode("utf-8"))
        functions = extract_functions(tree, source)
        self.assertEqual([function["name"] for function in functions], ["real_function"])

    @unittest.skipUnless(
        HAS_TREE_SITTER and is_cpp_available(),
        "tree-sitter-cpp is required for C++ function extraction tests",
    )
    def test_cpp_hosted_function_definitions_are_extracted(self):
        source = b"""\
extern "C" int cfunc() { return 1; }
namespace ns {
int nfunc() { return 2; }
}
class X {
public:
    int method() { return 3; }
    int declaration_only();
};
struct S {
    int smethod() { return 4; }
    int (*fp)(int);
};
"""
        path = Path("/tmp/py_cext_bugs_function_hosts.cpp")
        tree = parse_bytes_for_file(source, path)
        functions = extract_functions(tree, source)
        self.assertEqual(
            [function["name"] for function in functions],
            ["cfunc", "nfunc", "method", "smethod"],
        )


if __name__ == "__main__":
    unittest.main()
