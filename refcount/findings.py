#!/usr/bin/env python3
"""Post-processing helpers for refcount findings."""

import re


def deduplicate_findings(findings: list[dict]) -> list[dict]:
    """Deduplicate findings by type, file, and normalized detail."""

    def normalize(detail: str) -> str:
        text = re.sub(r"line \d+", "line N", detail)
        return re.sub(r"'[^']+?'", "'VAR'", text)

    groups: dict[tuple[str, str, str], list[dict]] = {}
    for finding in findings:
        key = (
            finding.get("type", ""),
            finding.get("file", ""),
            normalize(finding.get("detail", "")),
        )
        groups.setdefault(key, []).append(finding)

    result = []
    for group in groups.values():
        canonical = group[0]
        if len(group) > 1:
            canonical["duplicate_count"] = len(group) - 1
            canonical["duplicate_locations"] = [
                {"file": item.get("file", ""), "line": item.get("line", 0)}
                for item in group[1:]
            ]
        result.append(canonical)
    return result
