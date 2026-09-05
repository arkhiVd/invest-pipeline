#!/usr/bin/env python3
"""Reject broker mutation methods and route fragments in shipped Python code."""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

DENIED_ATTRIBUTES = {
    "place_order",
    "modify_order",
    "cancel_order",
    "place_gtt",
    "modify_gtt",
    "delete_gtt",
    "transfer_funds",
}
DENIED_ROUTES = re.compile(r"/(?:orders?|gtt|funds?/transfer)(?:/|\b)", re.I)


def scan(root: Path) -> list[str]:
    findings: list[str] = []
    for path in sorted((root / "invest").rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in DENIED_ATTRIBUTES:
                findings.append(f"denied broker method: {path.relative_to(root)}:{node.lineno}")
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if DENIED_ROUTES.search(node.value):
                    findings.append(f"denied broker route: {path.relative_to(root)}:{node.lineno}")
    return findings


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    findings = scan(root)
    if findings:
        print("broker safety scan: FAIL")
        print("\n".join(findings))
        return 1
    print("broker safety scan: PASS (0 findings)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
