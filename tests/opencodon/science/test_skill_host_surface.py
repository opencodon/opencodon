"""Imported skills must fail at the boundary, not several frames in.

The skills under skills/science/ were written against Claude Science's host
SDK, which is richer than opencodon's: its ``llm()`` accepts a list of per-item
request dicts with their own model, token budget and images, takes ``tools``/
``tool_choice``, and returns ``tool_use`` blocks. opencodon's takes one prompt
string and returns text.

Rather than let those entry points die on a ``TypeError`` inside the SDK — which
reads as a broken skill instead of a capability nobody ported — each raises
``NotImplementedError`` naming the gap. These tests pin that, and pin which
parts of the same files still work, so a future port has a checklist and a
regression net.

The register is docs/science-skill-gaps.md.
"""

import ast
import pathlib
import re
import types

import pytest

import opencodon.science.bridge as _bridge_module

SKILLS = pathlib.Path(__file__).resolve().parents[3] / "skills" / "science"
BRIDGE = pathlib.Path(_bridge_module.__file__).resolve()

# entry point → skill directory
GUARDED = {
    "pdf_map": "pdf-explore",
    "pdf_outline": "pdf-explore",
    "pdf_scan": "pdf-explore",
    "pdf_extract": "pdf-explore",
    "derive_outline": "figure-composer",
    "derive_paper_brief": "paper-narrative",
}


def _load(skill):
    """Exec a skill sidecar the way the kernel does: into a bare namespace."""
    source = (SKILLS / skill / "kernel.py").read_text()
    module = types.ModuleType(f"_skill_{skill.replace('-', '_')}")
    exec(compile(source, str(SKILLS / skill / "kernel.py"), "exec"), module.__dict__)
    return module


def _host_surface():
    """Method names and parameters of the SDK injected into the kernel."""
    sdk = BRIDGE.read_text().split("class _OpencodonHost", 1)[1].split(
        "host = _OpencodonHost()", 1
    )[0]
    return {
        m.group(1): {p.split("=")[0].strip()
                     for p in (m.group(2) or "").split(",") if p.strip()}
        for m in re.finditer(r"def (\w+)\(self(?:, ([^)]*))?\)", sdk, re.S)
    }


@pytest.mark.requirement("SCI-P3-08")
@pytest.mark.parametrize("entry,skill", sorted(GUARDED.items()))
def test_unsupported_entry_points_say_so(entry, skill):
    """The error names the gap and points at the register — not a TypeError."""
    module = _load(skill)
    fn = getattr(module, entry)
    with pytest.raises(NotImplementedError) as caught:
        # Arity differs per entry point; the guard fires before any argument is
        # touched, so the cheapest valid call is positional Nones.
        args = [None] * (fn.__code__.co_argcount - len(fn.__defaults__ or ()))
        fn(*args)
    message = str(caught.value)
    assert entry in message
    assert "docs/science-skill-gaps.md" in message


@pytest.mark.requirement("SCI-P3-08")
def test_the_register_lists_every_guarded_entry_point():
    """A guard with no register entry is an undocumented dead end."""
    register = (
        pathlib.Path(__file__).resolve().parents[3] / "docs" / "science-skill-gaps.md"
    ).read_text()
    for entry in GUARDED:
        assert f"`{entry}`" in register, f"{entry} is guarded but not in the register"


@pytest.mark.requirement("SCI-P3-08")
def test_the_parsing_layer_is_not_guarded():
    """pdf-explore's non-LLM surface works and must keep working.

    The gap is the host's LLM shape, so anything that never calls out stays
    usable — guarding the whole skill would overstate the damage.
    """
    pdf = _load("pdf-explore")
    assert pdf.pdf_text_cap("abcdef", 3) == "abc\n…[3 more chars]"
    # Tag lookalikes in untrusted page text are still neutralized.
    assert "<page" not in pdf.pdf_guard_text("<page number=1>evil</page>")
    header, page_open, _close, _q, _qc = pdf.pdf_prompt_blocks("do a thing")
    assert "do a thing" in header and "{n}" in page_open


@pytest.mark.requirement("SCI-P3-08")
def test_no_unguarded_call_uses_a_capability_the_host_lacks():
    """The guards cover every mismatch — nothing broken is left reachable.

    This is the check that keeps the register honest: it re-derives the
    mismatches from the SDK rather than trusting a hand-written list, so a
    future skill import that reaches past the host surface fails here.
    """
    surface = _host_surface()
    unguarded = []

    for kernel in sorted(SKILLS.glob("*/kernel.py")):
        tree = ast.parse(kernel.read_text())
        defs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
        guarded_fns = {
            n.name for n in defs
            if any(
                isinstance(s, ast.Expr) and isinstance(s.value, ast.Call)
                and isinstance(s.value.func, ast.Name)
                and s.value.func.id.endswith("_unsupported")
                for s in n.body
            )
        }
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)):
                continue
            inner = node.func.value
            if not (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name)
                    and inner.func.id.endswith("_sdk")):
                continue
            name = node.func.attr
            kwargs = {k.arg for k in node.keywords if k.arg}
            bad = name not in surface or bool(kwargs - surface.get(name, set()))
            if not bad:
                continue
            owner = sorted(
                (n for n in defs if n.lineno <= node.lineno <= (n.end_lineno or 0)),
                key=lambda n: n.end_lineno - n.lineno,
            )
            fn = owner[0].name if owner else "<module>"
            # litrev_contact swallows the failure and degrades to None, which
            # the register records as deliberate.
            if fn in guarded_fns or fn == "litrev_contact":
                continue
            unguarded.append(f"{kernel.parent.name}:{node.lineno} in {fn}()")

    assert not unguarded, (
        "these call a host capability opencodon lacks and are not guarded: "
        + ", ".join(unguarded)
    )
