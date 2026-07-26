"""AST-safe removal of test functions / classes from a pytest module.

Line-based regex removal broke a multi-line `def foo(\n args\n):` signature and
silently ate a class boundary earlier in this pass. ast.end_lineno gives the
exact span, so use this instead.
"""
import ast
import sys
from pathlib import Path


def drop(path, names):
    p = Path(path)
    src = p.read_text(encoding="utf-8")
    lines = src.splitlines(keepends=True)
    tree = ast.parse(src)
    want = set(names)
    spans, found = [], set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name in want:
            start = min([node.lineno] + [d.lineno for d in node.decorator_list]) - 1
            spans.append((start, node.end_lineno))
            found.add(node.name)
    # Drop spans nested inside another selected span — deleting both would
    # misalign the line offsets and silently truncate a neighbouring body.
    spans.sort()
    merged = []
    for start, end in spans:
        if merged and start >= merged[-1][0] and end <= merged[-1][1]:
            continue
        merged.append((start, end))
    for start, end in sorted(merged, reverse=True):
        del lines[start:end]
    out = "".join(lines)
    ast.parse(out)  # refuse to write a broken file
    p.write_text(out, encoding="utf-8")
    missing = want - found
    print(f"  {path}: removed {len(found)}" + (f" MISSING={sorted(missing)}" if missing else ""))
    return not missing


if __name__ == "__main__":
    drop(sys.argv[1], sys.argv[2:])
