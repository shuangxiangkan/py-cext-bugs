"""Structured ownership state for CPython C API refcount analysis."""

from dataclasses import dataclass, field


UNKNOWN = "unknown"
NULL = "null"
OWNED = "owned"
BORROWED = "borrowed"
RELEASED = "released"
STOLEN = "stolen"
RETURNED = "returned"
ESCAPED = "escaped"
MIXED = "mixed"

OWNERSHIP_STATES = frozenset(
    {
        UNKNOWN,
        NULL,
        OWNED,
        BORROWED,
        RELEASED,
        STOLEN,
        RETURNED,
        ESCAPED,
        MIXED,
    }
)


@dataclass(frozen=True)
class RefOwnership:
    """Ownership state for one C variable."""

    state: str
    line: int | None = None
    api: str | None = None
    alternatives: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if self.state not in OWNERSHIP_STATES:
            raise ValueError(f"unknown ownership state: {self.state}")

    @property
    def maybe_owned(self) -> bool:
        """Return true if the value may still require a DECREF."""
        if self.state == OWNED:
            return True
        return self.state == MIXED and OWNED in self.alternatives


@dataclass(frozen=True)
class OwnershipState:
    """Immutable map from variable name to ownership state."""

    variables: tuple[tuple[str, RefOwnership], ...] = ()
    aliases: tuple[tuple[str, str], ...] = ()

    def get(self, variable: str) -> RefOwnership:
        """Return ownership for a variable, defaulting to unknown."""
        data = dict(self.variables)
        return data.get(self.resolve(variable), RefOwnership(UNKNOWN))

    def has(self, variable: str) -> bool:
        """Return true if a variable or alias is tracked."""
        return variable in dict(self.variables) or variable in dict(self.aliases)

    def resolve(self, variable: str) -> str:
        """Return the canonical variable for a simple alias chain."""
        aliases = dict(self.aliases)
        seen = set()
        current = variable
        while current in aliases and current not in seen:
            seen.add(current)
            current = aliases[current]
        return current

    def set(self, variable: str, ownership: RefOwnership) -> "OwnershipState":
        """Return a new state with one variable updated."""
        data = dict(self.variables)
        data[variable] = ownership
        aliases = dict(self.aliases)
        aliases.pop(variable, None)
        return OwnershipState(tuple(sorted(data.items())), tuple(sorted(aliases.items())))

    def mark(
        self,
        variable: str,
        state: str,
        *,
        line: int | None = None,
        api: str | None = None,
    ) -> "OwnershipState":
        """Return a new state with one variable marked."""
        return self.set(variable, RefOwnership(state, line=line, api=api))

    def mark_reference(
        self,
        variable: str,
        state: str,
        *,
        line: int | None = None,
        api: str | None = None,
    ) -> "OwnershipState":
        """Return a new state with a variable or its alias target marked."""
        return self.set(self.resolve(variable), RefOwnership(state, line=line, api=api))

    def alias(self, variable: str, target: str) -> "OwnershipState":
        """Return a new state where variable aliases target."""
        target = self.resolve(target)
        if variable == target:
            return self.drop(variable)

        data = dict(self.variables)
        data.pop(variable, None)
        aliases = dict(self.aliases)
        aliases[variable] = target
        return OwnershipState(tuple(sorted(data.items())), tuple(sorted(aliases.items())))

    def drop(self, variable: str) -> "OwnershipState":
        """Return a new state without one variable."""
        data = dict(self.variables)
        data.pop(variable, None)
        aliases = dict(self.aliases)
        aliases.pop(variable, None)
        return OwnershipState(tuple(sorted(data.items())), tuple(sorted(aliases.items())))

    def owned_variables(self) -> dict[str, RefOwnership]:
        """Return variables that may still require a DECREF."""
        return {
            name: ownership
            for name, ownership in self.variables
            if ownership.maybe_owned
        }


def merge_ownership_states(states: list[OwnershipState]) -> OwnershipState:
    """Merge ownership states from multiple CFG predecessors."""
    if not states:
        return OwnershipState()

    maps = [dict(state.variables) for state in states]
    names = sorted({name for variables in maps for name in variables})
    unknown = RefOwnership(UNKNOWN)
    merged = {
        name: merge_ref_ownership([variables.get(name, unknown) for variables in maps])
        for name in names
    }
    aliases = _merge_aliases(states)
    return OwnershipState(tuple(merged.items()), tuple(sorted(aliases.items())))


def merge_ref_ownership(values: list[RefOwnership]) -> RefOwnership:
    """Merge ownership for one variable from multiple paths."""
    if not values:
        return RefOwnership(UNKNOWN)

    first = values[0]
    if all(value == first for value in values):
        return first

    states = frozenset(_flatten_states(value) for value in values)
    alternatives = frozenset().union(*states)
    if len(alternatives) == 1:
        return RefOwnership(next(iter(alternatives)))
    return RefOwnership(MIXED, alternatives=alternatives)


def _flatten_states(value: RefOwnership) -> frozenset[str]:
    if value.state == MIXED:
        return value.alternatives or frozenset({UNKNOWN})
    return frozenset({value.state})


def _merge_aliases(states: list[OwnershipState]) -> dict[str, str]:
    if not states:
        return {}

    alias_maps = [dict(state.aliases) for state in states]
    names = sorted({name for aliases in alias_maps for name in aliases})
    result = {}
    for name in names:
        targets = {aliases.get(name) for aliases in alias_maps}
        if len(targets) == 1:
            target = targets.pop()
            if target is not None:
                result[name] = target
    return result
