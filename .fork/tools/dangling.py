"""Report Name loads in changed files that resolve to nothing at module scope.

Neither `ruff` nor `import <module>` evaluates names inside function bodies, so
removing a function can leave a NameError that only fires at call time. This
walks each file's own scopes and reports Load-context names with no binding.
"""
import ast, builtins, subprocess, sys

BUILTINS = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__package__", "_"}

def bindings(tree):
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(n.name)
        elif isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            out.add(n.id)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                out.add((a.asname or a.name).split(".")[0])
        elif isinstance(n, ast.arg):
            out.add(n.arg)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            out.add(n.name)
        elif isinstance(n, ast.Global) or isinstance(n, ast.Nonlocal):
            out.update(n.names)
        elif isinstance(n, (ast.comprehension,)):
            for t in ast.walk(n.target):
                if isinstance(t, ast.Name):
                    out.add(t.id)
        elif isinstance(n, ast.MatchAs) and n.name:
            out.add(n.name)
        elif isinstance(n, ast.MatchStar) and n.name:
            out.add(n.name)
    return out

files = sys.argv[1:] or subprocess.run(
    ["git", "diff", "--name-only", "HEAD"], capture_output=True, text=True, check=True
).stdout.split()
bad = 0
for f in files:
    if not f.endswith(".py"):
        continue
    try:
        src = open(f, encoding="utf-8").read()
        tree = ast.parse(src)
    except Exception as e:
        print(f"PARSE {f}: {e}"); bad += 1; continue
    defined = bindings(tree) | BUILTINS
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id not in defined:
            print(f"DANGLING {f}:{n.lineno}: {n.id}")
            bad += 1
print("issues:", bad)
sys.exit(1 if bad else 0)
