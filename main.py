#!/usr/bin/env python3
"""Command-line entry point for py-cext-bugs tools."""

import argparse
import sys

if not __package__:
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent))


def parse_subcommand_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command")

    refcount = subparsers.add_parser("refcount", help="Run refcount analysis")
    refcount.add_argument("target", nargs="?", default=".")
    refcount.add_argument("--max-files", type=int, default=0)
    refcount.add_argument("--api-tables")
    refcount.add_argument("--no-comment-suppression", action="store_true")

    discover = subparsers.add_parser(
        "discover",
        help="Heuristically discover CPython C extension source files",
    )
    discover.add_argument("target", nargs="?", default=".")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    if not argv or argv[0] not in {"discover", "refcount"}:
        import refcount.analyzer as refcount_analyzer

        return refcount_analyzer.main(argv or ["."])

    args = parse_subcommand_args(argv)
    if args.command == "discover":
        import refcount.c_extension as c_extension

        return c_extension.main([args.target])
    if args.command == "refcount":
        import refcount.analyzer as refcount_analyzer

        refcount_args = [args.target]
        if args.max_files:
            refcount_args.extend(["--max-files", str(args.max_files)])
        if args.api_tables:
            refcount_args.extend(["--api-tables", args.api_tables])
        if args.no_comment_suppression:
            refcount_args.append("--no-comment-suppression")
        return refcount_analyzer.main(refcount_args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
