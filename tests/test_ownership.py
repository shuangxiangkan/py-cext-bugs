"""Tests for structured refcount ownership state."""

import sys
import unittest
from pathlib import Path

TOOL_ROOT = Path(__file__).resolve().parents[1]
if str(TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOL_ROOT))

from refcount.ownership_state import (
    BORROWED,
    MIXED,
    NULL,
    OWNED,
    RELEASED,
    UNKNOWN,
    OwnershipState,
    RefOwnership,
    merge_ownership_states,
    merge_ref_ownership,
)


class TestRefOwnership(unittest.TestCase):
    """Test one-variable ownership values."""

    def test_rejects_unknown_state_name(self):
        with self.assertRaises(ValueError):
            RefOwnership("not-a-state")

    def test_owned_and_mixed_maybe_owned(self):
        self.assertTrue(RefOwnership(OWNED).maybe_owned)
        self.assertTrue(
            RefOwnership(MIXED, alternatives=frozenset({OWNED, RELEASED})).maybe_owned
        )
        self.assertFalse(RefOwnership(RELEASED).maybe_owned)
        self.assertFalse(
            RefOwnership(MIXED, alternatives=frozenset({BORROWED, RELEASED})).maybe_owned
        )

    def test_merge_identical_values_preserves_metadata(self):
        value = RefOwnership(OWNED, line=12, api="PyList_New")

        merged = merge_ref_ownership([value, value])

        self.assertEqual(merged, value)

    def test_merge_different_values_becomes_mixed(self):
        merged = merge_ref_ownership(
            [
                RefOwnership(OWNED, line=4, api="PyList_New"),
                RefOwnership(RELEASED, line=8, api="Py_DECREF"),
            ]
        )

        self.assertEqual(merged.state, MIXED)
        self.assertEqual(merged.alternatives, frozenset({OWNED, RELEASED}))


class TestOwnershipState(unittest.TestCase):
    """Test immutable variable ownership maps."""

    def test_missing_variable_is_unknown(self):
        state = OwnershipState()

        self.assertEqual(state.get("obj"), RefOwnership(UNKNOWN))

    def test_mark_returns_new_state(self):
        state = OwnershipState()

        updated = state.mark("obj", OWNED, line=5, api="PyDict_New")

        self.assertEqual(state.get("obj").state, UNKNOWN)
        self.assertEqual(updated.get("obj"), RefOwnership(OWNED, 5, "PyDict_New"))

    def test_drop_removes_variable(self):
        state = OwnershipState().mark("obj", BORROWED)

        updated = state.drop("obj")

        self.assertEqual(state.get("obj").state, BORROWED)
        self.assertEqual(updated.get("obj").state, UNKNOWN)

    def test_alias_reads_target_ownership(self):
        state = OwnershipState().mark("obj", OWNED).alias("alias", "obj")

        self.assertEqual(state.resolve("alias"), "obj")
        self.assertEqual(state.get("alias").state, OWNED)

    def test_mark_reference_updates_alias_target(self):
        state = OwnershipState().mark("obj", OWNED).alias("alias", "obj")

        updated = state.mark_reference("alias", RELEASED, line=7, api="Py_DECREF")

        self.assertEqual(updated.get("obj"), RefOwnership(RELEASED, 7, "Py_DECREF"))
        self.assertEqual(updated.get("alias"), RefOwnership(RELEASED, 7, "Py_DECREF"))

    def test_direct_mark_clears_alias(self):
        state = OwnershipState().mark("obj", OWNED).alias("alias", "obj")

        updated = state.mark("alias", BORROWED)

        self.assertEqual(updated.resolve("alias"), "alias")
        self.assertEqual(updated.get("alias").state, BORROWED)
        self.assertEqual(updated.get("obj").state, OWNED)

    def test_owned_variables_includes_maybe_owned(self):
        state = (
            OwnershipState()
            .mark("owned", OWNED)
            .mark("null_obj", NULL)
            .set("mixed", RefOwnership(MIXED, alternatives=frozenset({OWNED, NULL})))
        )

        self.assertEqual(set(state.owned_variables()), {"owned", "mixed"})

    def test_merge_states_preserves_same_value(self):
        left = OwnershipState().mark("obj", OWNED, line=5, api="PyList_New")
        right = OwnershipState().mark("obj", OWNED, line=5, api="PyList_New")

        merged = merge_ownership_states([left, right])

        self.assertEqual(merged.get("obj"), left.get("obj"))

    def test_merge_states_marks_path_disagreement(self):
        left = OwnershipState().mark("obj", OWNED)
        right = OwnershipState().mark("obj", RELEASED)

        merged = merge_ownership_states([left, right])

        self.assertEqual(merged.get("obj").state, MIXED)
        self.assertEqual(merged.get("obj").alternatives, frozenset({OWNED, RELEASED}))

    def test_merge_missing_variable_as_unknown(self):
        left = OwnershipState().mark("obj", OWNED)
        right = OwnershipState()

        merged = merge_ownership_states([left, right])

        self.assertEqual(merged.get("obj").state, MIXED)
        self.assertEqual(merged.get("obj").alternatives, frozenset({OWNED, UNKNOWN}))

    def test_merge_preserves_alias_when_all_paths_agree(self):
        left = OwnershipState().mark("obj", OWNED).alias("alias", "obj")
        right = OwnershipState().mark("obj", OWNED).alias("alias", "obj")

        merged = merge_ownership_states([left, right])

        self.assertEqual(merged.resolve("alias"), "obj")
        self.assertEqual(merged.get("alias").state, OWNED)

    def test_merge_drops_alias_when_paths_disagree(self):
        left = OwnershipState().mark("obj", OWNED).alias("alias", "obj")
        right = OwnershipState().mark("obj", OWNED)

        merged = merge_ownership_states([left, right])

        self.assertEqual(merged.resolve("alias"), "alias")


if __name__ == "__main__":
    unittest.main()
