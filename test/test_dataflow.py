"""Tests for the generic forward data-flow solver."""

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
    from extract.cfg import build_function_cfg
    from extract.dataflow import analyze_forward
    from extract.tree_sitter_extractor import extract_functions, parse_string
else:
    build_function_cfg = None
    analyze_forward = None
    extract_functions = None
    parse_string = None


SEQUENCE_SAMPLE = """\
static int
simple(void)
{
    int x = 0;
    x++;
    return x;
}
"""


IF_ELSE_SAMPLE = """\
static void
join(int flag)
{
    if (flag) {
        a();
    } else {
        b();
    }
    c();
}
"""


IF_WITHOUT_ELSE_SAMPLE = """\
static int
branch(int fail)
{
    if (fail)
        return -1;
    ok();
    return 0;
}
"""


GOTO_SAMPLE = """\
static int
jump(void)
{
    goto out;
    bad();
out:
    return 0;
}
"""


LOOP_SAMPLE = """\
static int
loop(int n)
{
    int i = 0;
    while (i < n) {
        i++;
    }
    return i;
}
"""


@unittest.skipUnless(
    HAS_TREE_SITTER,
    "tree-sitter and tree-sitter-c are required for data-flow tests",
)
class TestForwardDataFlow(unittest.TestCase):
    """Test fixed-point propagation over the CFG."""

    def _build_cfg(self, code):
        source_bytes = code.encode("utf-8")
        tree = parse_string(code)
        functions = extract_functions(tree, source_bytes)
        self.assertEqual(len(functions), 1)
        return build_function_cfg(functions[0], source_bytes)

    def _node_containing(self, cfg, text, kind=None):
        matches = [node for node in cfg.nodes if text in node.text]
        if kind is not None:
            matches = [node for node in matches if node.kind == kind]
        self.assertEqual(len(matches), 1)
        return matches[0]

    def _merge_sets(self, states):
        result = frozenset()
        for state in states:
            result = result | state
        return result

    def _record_statement_text(self, node, state):
        if node.kind in {"statement", "declaration", "return"}:
            return state | frozenset({node.text})
        return state

    def test_sequential_facts_reach_return(self):
        cfg = self._build_cfg(SEQUENCE_SAMPLE)
        ret = self._node_containing(cfg, "return x;", "return")

        result = analyze_forward(
            cfg,
            frozenset(),
            self._record_statement_text,
            self._merge_sets,
        )

        self.assertIn("int x = 0;", result.in_states[ret.id])
        self.assertIn("x++;", result.in_states[ret.id])

    def test_branch_facts_join_at_following_statement(self):
        cfg = self._build_cfg(IF_ELSE_SAMPLE)
        join = self._node_containing(cfg, "c();", "statement")

        result = analyze_forward(
            cfg,
            frozenset(),
            self._record_statement_text,
            self._merge_sets,
        )

        self.assertIn("a();", result.in_states[join.id])
        self.assertIn("b();", result.in_states[join.id])

    def test_edge_transfer_refines_true_and_false_paths(self):
        cfg = self._build_cfg(IF_WITHOUT_ELSE_SAMPLE)
        early_return = self._node_containing(cfg, "return -1;", "return")
        ok_call = self._node_containing(cfg, "ok();", "statement")

        def refine(edge, state):
            source = cfg.nodes[edge.source]
            if source.kind == "if":
                return state | frozenset({f"branch:{edge.kind}"})
            return state

        result = analyze_forward(
            cfg,
            frozenset(),
            lambda node, state: state,
            self._merge_sets,
            edge_transfer=refine,
        )

        self.assertIn("branch:true", result.in_states[early_return.id])
        self.assertNotIn("branch:false", result.in_states[early_return.id])
        self.assertIn("branch:false", result.in_states[ok_call.id])
        self.assertNotIn("branch:true", result.in_states[ok_call.id])

    def test_unreachable_statement_has_no_input_state(self):
        cfg = self._build_cfg(GOTO_SAMPLE)
        bad_call = self._node_containing(cfg, "bad();", "statement")
        ret = self._node_containing(cfg, "return 0;", "return")

        result = analyze_forward(
            cfg,
            frozenset(),
            self._record_statement_text,
            self._merge_sets,
        )

        self.assertNotIn(bad_call.id, result.in_states)
        self.assertIn(ret.id, result.in_states)

    def test_loop_analysis_reaches_fixed_point(self):
        cfg = self._build_cfg(LOOP_SAMPLE)
        loop = next(node for node in cfg.nodes if node.kind == "loop")
        increment = self._node_containing(cfg, "i++;", "statement")
        ret = self._node_containing(cfg, "return i;", "return")

        result = analyze_forward(
            cfg,
            frozenset(),
            self._record_statement_text,
            self._merge_sets,
        )

        self.assertIn("int i = 0;", result.in_states[loop.id])
        self.assertIn("i++;", result.in_states[loop.id])
        self.assertIn("i++;", result.in_states[ret.id])
        self.assertGreaterEqual(result.iterations, len(cfg.nodes))


if __name__ == "__main__":
    unittest.main()
