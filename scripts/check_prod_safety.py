#!/usr/bin/env python3
"""Scan ``agent_kernel/`` for PoC/mock/stub/placeholder implementation markers.

The script is intentionally lightweight and uses only the Python standard
library. It reports line-level findings for suspicious implementation wording
and supports allowlists supplied either directly on the command line or via a
plain-text file.

Allowlist entries can be written in any of these forms:

* ``path:agent_kernel/kernel/**/*.py`` - file glob matched against the
  repository-relative path.
* ``glob:agent_kernel/kernel/**/*.py`` - same as ``path:``.
* ``text:PoC/test-only`` - substring matched against the offending line.
* ``agent_kernel/kernel/**/*.py`` - inferred as a path glob when it contains
  wildcard or path separator characters.
* ``PoC/test-only`` - inferred as a text fragment otherwise.
"""

from __future__ import annotations

import argparse
import fnmatch
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

ROOT_DIR = Path("agent_kernel")
TEXT_SUFFIXES = {".py"}

KEYWORD_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("poc", re.compile(r"\bPoC\b", re.IGNORECASE)),
    ("test-only", re.compile(r"\btest[- ]only\b", re.IGNORECASE)),
    ("mock", re.compile(r"\bmock(?:ed|ing|s)?\b|\bMock[A-Za-z0-9_]+\b")),
    ("stub", re.compile(r"\bstub(?:s|bed|bing)?\b|\bStub[A-Za-z0-9_]+\b", re.IGNORECASE)),
    ("placeholder", re.compile(r"\bplaceholder\b", re.IGNORECASE)),
)


@dataclass(frozen=True, slots=True)
class AllowRule:
    kind: str
    value: str


@dataclass(frozen=True, slots=True)
class Finding:
    path: Path
    line_number: int
    keyword: str
    line: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check agent_kernel for PoC/mock/stub/placeholder implementations."
    )
    parser.add_argument(
        "--allow",
        action="append",
        default=[],
        help=(
            "Allow a file glob or text fragment. Repeatable. "
            "Use path:/glob: for file globs and text: for line fragments."
        ),
    )
    parser.add_argument(
        "--allowlist-file",
        action="append",
        default=[],
        help="Read additional allowlist entries from a text file. Repeatable.",
    )
    return parser.parse_args()


def iter_python_files(root: Path) -> Iterable[Path]:
    for path in root.rglob("*.py"):
        if any(part == "__pycache__" or part.startswith(".") for part in path.parts):
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        if path.is_file():
            yield path


def normalize_path(path: Path) -> str:
    return path.as_posix()


def looks_like_path_pattern(value: str) -> bool:
    return any(ch in value for ch in ("*", "?", "[", "]", "/", "\\"))


def parse_allow_entry(raw: str) -> list[AllowRule]:
    entry = raw.strip().lstrip("\ufeff")
    if not entry or entry.startswith("#"):
        return []
    if entry.startswith("path:"):
        return [AllowRule("path", entry.removeprefix("path:").strip().replace("\\", "/"))]
    if entry.startswith("glob:"):
        return [AllowRule("path", entry.removeprefix("glob:").strip().replace("\\", "/"))]
    if entry.startswith("text:"):
        return [AllowRule("text", entry.removeprefix("text:").strip())]
    if looks_like_path_pattern(entry):
        return [AllowRule("path", entry.replace("\\", "/"))]
    return [AllowRule("text", entry)]


def load_allow_rules(allow_values: list[str], allowlist_files: list[str]) -> list[AllowRule]:
    rules: list[AllowRule] = []
    for value in allow_values:
        rules.extend(parse_allow_entry(value))
    for file_name in allowlist_files:
        file_path = Path(file_name)
        try:
            lines = file_path.read_text(encoding="utf-8-sig").splitlines()
        except FileNotFoundError:
            raise SystemExit(f"Allowlist file not found: {file_path}") from None
        for line in lines:
            rules.extend(parse_allow_entry(line))
    return rules


def path_is_allowed(path: Path, rules: list[AllowRule]) -> bool:
    rel_path = normalize_path(path)
    return any(rule.kind == "path" and fnmatch.fnmatch(rel_path, rule.value) for rule in rules)


def line_is_allowed(line: str, rules: list[AllowRule]) -> bool:
    return any(rule.kind == "text" and rule.value in line for rule in rules)


def scan_file(path: Path, rules: list[AllowRule]) -> list[Finding]:
    if path_is_allowed(path, rules):
        return []

    findings: list[Finding] = []
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise SystemExit(f"Failed to read {path}: {exc}") from exc

    for line_number, line in enumerate(content.splitlines(), start=1):
        if line_is_allowed(line, rules):
            continue
        for keyword, pattern in KEYWORD_PATTERNS:
            if pattern.search(line):
                findings.append(
                    Finding(
                        path=path,
                        line_number=line_number,
                        keyword=keyword,
                        line=line.rstrip(),
                    )
                )
    return findings


def format_finding(finding: Finding) -> str:
    return (
        f"{normalize_path(finding.path)}:{finding.line_number} [{finding.keyword}] {finding.line}"
    )


def main() -> int:
    args = parse_args()
    allow_rules = load_allow_rules(args.allow, args.allowlist_file)

    if not ROOT_DIR.exists():
        print(f"Root directory not found: {ROOT_DIR}", file=sys.stderr)
        return 2

    scanned_files = 0
    findings: list[Finding] = []
    for path in iter_python_files(ROOT_DIR):
        scanned_files += 1
        findings.extend(scan_file(path, allow_rules))

    if findings:
        print(
            f"Found {len(findings)} potential production-safety issue(s) "
            f"across {scanned_files} Python file(s) under {ROOT_DIR}/.",
            file=sys.stderr,
        )
        for finding in sorted(
            findings,
            key=lambda item: (normalize_path(item.path), item.line_number, item.keyword),
        ):
            print(format_finding(finding), file=sys.stderr)
        return 1

    print(
        "OK: scanned "
        f"{scanned_files} Python file(s) under {ROOT_DIR}/ "
        "and found no disallowed markers."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
