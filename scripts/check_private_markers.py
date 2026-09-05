#!/usr/bin/env python3
"""Fail when public-candidate files contain private markers or financial artifacts."""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

TEXT_PATTERNS = {
    "private absolute path": re.compile(r"/(?:home|opt/homelab)/", re.I),
    "private hostname": re.compile(r"\belitedesk\b", re.I),
    "email address": re.compile(r"\b[\w.+-]+@(?!example\.invalid\b)[\w.-]+\.[A-Za-z]{2,}\b"),
    "private IPv4 address": re.compile(
        r"\b(?!(?:127\.0\.0\.1|0\.0\.0\.0)\b)(?:10\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.)\d{1,3}(?:\.\d{1,3}){2}\b"
    ),
}
DENIED_SUFFIXES = {".csv", ".xls", ".xlsx", ".ods", ".db", ".duckdb", ".sqlite", ".parquet", ".log"}
SKIP_PARTS = {".git", ".venv", ".pytest_cache", ".ruff_cache", "__pycache__"}


def tracked_files(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True
    )
    return [root / name.decode() for name in result.stdout.split(b"\0") if name]


def scan(root: Path) -> list[str]:
    findings: list[str] = []
    for path in tracked_files(root):
        relative = path.relative_to(root)
        if any(part in SKIP_PARTS for part in relative.parts):
            continue
        if path.suffix.lower() in DENIED_SUFFIXES:
            findings.append(f"denied financial artifact: {relative}")
            continue
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        for label, pattern in TEXT_PATTERNS.items():
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                findings.append(f"{label}: {relative}:{line}")
    return findings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", default=".")
    args = parser.parse_args()
    findings = scan(Path(args.root).resolve())
    if findings:
        print("private-marker scan: FAIL")
        print("\n".join(findings))
        return 1
    print("private-marker scan: PASS (0 findings)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
