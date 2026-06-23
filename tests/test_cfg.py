"""Tests for the lightweight C control-flow graph builder."""

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
    from analysis.controlflow import build_function_cfg
    from analysis.parsing import extract_functions, parse_string
else:
    build_function_cfg = None
    extract_functions = None
    parse_string = None


CFG_SAMPLE = """\
static PyObject *
demo(PyObject *self)
{
    PyObject *a = PyList_New(0);
    if (a == NULL)
        return NULL;

    PyObject *b = PyDict_New();
    if (b == NULL)
        goto error;

    Py_DECREF(a);
    return b;

error:
    Py_XDECREF(a);
    return NULL;
}
"""


LOOP_SAMPLE = """\
static PyObject *
loopy(PyObject *self, PyObject *seq, Py_ssize_t n)
{
    PyObject *acc = PyList_New(0);
    for (Py_ssize_t i = 0; i < n; i++) {
        PyObject *item = PySequence_GetItem(seq, i);
        if (item == NULL)
            break;
        if (PyList_Append(acc, item) < 0) {
            Py_DECREF(item);
            return NULL;
        }
        Py_DECREF(item);
        continue;
    }
    return acc;
}
"""


DO_WHILE_SAMPLE = """\
static int
spin(int n)
{
    int i = 0;
    do {
        i++;
    } while (i < n);
    return i;
}
"""


SIMPLE_SEQUENCE_SAMPLE = """\
static int
simple(void)
{
    int x = 0;
    x++;
    return x;
}
"""


IF_WITHOUT_ELSE_SAMPLE = """\
static PyObject *
maybe_return(PyObject *self, int fail)
{
    if (fail)
        return NULL;
    Py_INCREF(self);
    return self;
}
"""


IF_ELSE_JOIN_SAMPLE = """\
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


GOTO_FALLTHROUGH_SAMPLE = """\
static int
jump(void)
{
    goto out;
    bad();
out:
    return 0;
}
"""


UNRESOLVED_GOTO_SAMPLE = """\
static int
broken(void)
{
    goto missing;
    return 0;
}
"""


SWITCH_SAMPLE = """\
static int
opaque_switch(int x)
{
    switch (x) {
    case 1:
        return 1;
    default:
        break;
    }
    return 0;
}
"""


@unittest.skipUnless(
    HAS_TREE_SITTER,
    "tree-sitter and tree-sitter-c are required for CFG tests",
)
class TestControlFlowGraph(unittest.TestCase):
    """Test the statement-level CFG builder."""

    def _build_cfg(self, code: str = CFG_SAMPLE):
        source_bytes = code.encode("utf-8")
        tree = parse_string(code)
        functions = extract_functions(tree, source_bytes)
        self.assertEqual(len(functions), 1)
        return build_function_cfg(functions[0], source_bytes)

    def _node_containing(self, cfg, text: str, kind: str | None = None):
        matches = [node for node in cfg.nodes if text in node.text]
        if kind is not None:
            matches = [node for node in matches if node.kind == kind]
        self.assertEqual(len(matches), 1)
        return matches[0]

    def _has_edge(self, cfg, source_id: int, target_id: int, kind: str) -> bool:
        return any(
            edge.target == target_id and edge.kind == kind
            for edge in cfg.successors(source_id)
        )

    def test_builds_entry_exit_and_labels(self):
        cfg = self._build_cfg()

        self.assertEqual(cfg.nodes[cfg.entry_id].kind, "entry")
        self.assertEqual(cfg.nodes[cfg.exit_id].kind, "exit")
        self.assertIn("error", cfg.labels)
        self.assertEqual(cfg.nodes[cfg.labels["error"]].kind, "label")

    def test_resolves_goto_to_label(self):
        cfg = self._build_cfg()

        goto_nodes = [node for node in cfg.nodes if node.kind == "goto"]
        self.assertEqual(len(goto_nodes), 1)
        self.assertEqual(goto_nodes[0].goto_label, "error")
        self.assertEqual(cfg.unresolved_gotos, {})

        goto_edges = [
            edge
            for edge in cfg.edges
            if edge.source == goto_nodes[0].id and edge.kind == "goto"
        ]
        self.assertEqual(len(goto_edges), 1)
        self.assertEqual(goto_edges[0].target, cfg.labels["error"])

    def test_if_nodes_have_true_and_false_edges(self):
        cfg = self._build_cfg()

        if_nodes = [node for node in cfg.nodes if node.kind == "if"]
        self.assertEqual(len(if_nodes), 2)
        for node in if_nodes:
            kinds = {edge.kind for edge in cfg.successors(node.id)}
            self.assertIn("true", kinds)
            self.assertIn("false", kinds)

    def test_return_edges_point_to_exit(self):
        cfg = self._build_cfg()

        return_nodes = [node for node in cfg.nodes if node.kind == "return"]
        self.assertEqual(len(return_nodes), 3)
        for node in return_nodes:
            return_edges = [
                edge
                for edge in cfg.successors(node.id)
                if edge.kind == "return"
            ]
            self.assertEqual(len(return_edges), 1)
            self.assertEqual(return_edges[0].target, cfg.exit_id)

    def test_sequential_statements_fall_through_in_order(self):
        cfg = self._build_cfg(SIMPLE_SEQUENCE_SAMPLE)

        declaration = self._node_containing(cfg, "int x = 0;", "declaration")
        increment = self._node_containing(cfg, "x++;", "statement")
        ret = self._node_containing(cfg, "return x;", "return")

        self.assertEqual(declaration.kind, "declaration")
        self.assertEqual(increment.kind, "statement")
        self.assertTrue(self._has_edge(cfg, cfg.entry_id, declaration.id, "entry"))
        self.assertTrue(self._has_edge(cfg, declaration.id, increment.id, "next"))
        self.assertTrue(self._has_edge(cfg, increment.id, ret.id, "next"))

    def test_if_without_else_false_branch_falls_through(self):
        cfg = self._build_cfg(IF_WITHOUT_ELSE_SAMPLE)

        if_node = self._node_containing(cfg, "if (fail)", "if")
        null_return = self._node_containing(cfg, "return NULL;", "return")
        incref = self._node_containing(cfg, "Py_INCREF(self);", "statement")

        self.assertTrue(self._has_edge(cfg, if_node.id, null_return.id, "true"))
        self.assertTrue(self._has_edge(cfg, if_node.id, incref.id, "false"))
        self.assertFalse(self._has_edge(cfg, null_return.id, incref.id, "next"))

    def test_if_else_branches_join_following_statement(self):
        cfg = self._build_cfg(IF_ELSE_JOIN_SAMPLE)

        true_call = self._node_containing(cfg, "a();", "statement")
        false_call = self._node_containing(cfg, "b();", "statement")
        join_call = self._node_containing(cfg, "c();", "statement")

        self.assertTrue(self._has_edge(cfg, true_call.id, join_call.id, "next"))
        self.assertTrue(self._has_edge(cfg, false_call.id, join_call.id, "next"))

    def test_goto_does_not_fall_through_to_next_statement(self):
        cfg = self._build_cfg(GOTO_FALLTHROUGH_SAMPLE)

        goto_node = self._node_containing(cfg, "goto out;", "goto")
        bad_call = self._node_containing(cfg, "bad();", "statement")
        label_node = cfg.nodes[cfg.labels["out"]]
        out_edges = cfg.successors(goto_node.id)

        self.assertEqual(len(out_edges), 1)
        self.assertEqual(out_edges[0].kind, "goto")
        self.assertEqual(out_edges[0].target, label_node.id)
        self.assertNotIn(bad_call.id, {edge.target for edge in out_edges})

    def test_unresolved_goto_is_reported(self):
        cfg = self._build_cfg(UNRESOLVED_GOTO_SAMPLE)

        goto_node = self._node_containing(cfg, "goto missing;", "goto")
        self.assertEqual(cfg.unresolved_gotos, {goto_node.id: "missing"})
        self.assertEqual(cfg.successors(goto_node.id), [])

    def test_switch_is_opaque_statement(self):
        cfg = self._build_cfg(SWITCH_SAMPLE)

        switch_node = self._node_containing(cfg, "switch (x)", "statement")
        final_return = self._node_containing(cfg, "return 0;", "return")
        return_nodes = [node for node in cfg.nodes if node.kind == "return"]

        self.assertEqual(switch_node.kind, "statement")
        self.assertEqual(return_nodes, [final_return])
        self.assertTrue(self._has_edge(cfg, switch_node.id, final_return.id, "next"))


@unittest.skipUnless(
    HAS_TREE_SITTER,
    "tree-sitter and tree-sitter-c are required for CFG tests",
)
class TestLoops(unittest.TestCase):
    """Test loop, break, and continue handling."""

    def _build(self, code):
        source_bytes = code.encode("utf-8")
        tree = parse_string(code)
        functions = extract_functions(tree, source_bytes)
        return build_function_cfg(functions[0], source_bytes)

    def test_for_loop_body_is_expanded(self):
        cfg = self._build(LOOP_SAMPLE)

        # The return nested inside the loop body must reach exit.
        return_nodes = [node for node in cfg.nodes if node.kind == "return"]
        self.assertEqual(len(return_nodes), 2)
        for node in return_nodes:
            targets = {edge.target for edge in cfg.successors(node.id)}
            self.assertIn(cfg.exit_id, targets)

    def test_loop_header_has_back_edge(self):
        cfg = self._build(LOOP_SAMPLE)

        loop_nodes = [node for node in cfg.nodes if node.kind == "loop"]
        self.assertEqual(len(loop_nodes), 1)
        header = loop_nodes[0]
        # Header exits the loop on the false branch.
        succ_kinds = {edge.kind for edge in cfg.successors(header.id)}
        self.assertIn("false", succ_kinds)
        # Something loops back to the header (here via the explicit continue;
        # a fall-through body would loop back with a "back" edge instead).
        back = [
            edge
            for edge in cfg.predecessors(header.id)
            if edge.kind in ("back", "continue")
        ]
        self.assertTrue(back)

    def test_break_targets_after_loop(self):
        cfg = self._build(LOOP_SAMPLE)

        break_nodes = [node for node in cfg.nodes if node.kind == "break"]
        self.assertEqual(len(break_nodes), 1)
        # Break is a loop exit, so it flows to the same place as the header's
        # false branch (the statement following the loop), not back to it.
        out = cfg.successors(break_nodes[0].id)
        self.assertEqual([edge.kind for edge in out], ["break"])

    def test_continue_loops_back_to_header(self):
        cfg = self._build(LOOP_SAMPLE)

        loop_id = next(node.id for node in cfg.nodes if node.kind == "loop")
        continue_nodes = [node for node in cfg.nodes if node.kind == "continue"]
        self.assertEqual(len(continue_nodes), 1)
        out = cfg.successors(continue_nodes[0].id)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].kind, "continue")
        self.assertEqual(out[0].target, loop_id)

    def test_do_while_body_runs_before_test(self):
        cfg = self._build(DO_WHILE_SAMPLE)

        loop_id = next(node.id for node in cfg.nodes if node.kind == "loop")
        # The loop header tests the condition; it should have a back edge into
        # the body and a false edge out of the loop.
        succ_kinds = {edge.kind for edge in cfg.successors(loop_id)}
        self.assertIn("back", succ_kinds)
        self.assertIn("false", succ_kinds)


if __name__ == "__main__":
    unittest.main()
