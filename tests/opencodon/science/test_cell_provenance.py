"""Failure-location and replayability provenance on ``execution_log``.

Three things a cell row has to carry that it previously did not: *where* it
broke (``error_lineno`` + ``traceback``), whether rich display output was
produced and dropped (``display_count``), and whether the recorded source is
plain-language-replayable or needs an IPython kernel (``has_magics``).
"""

import json
from pathlib import Path

import pytest

from opencodon.science.kernels import error_lineno_from_traceback, traceback_text
from opencodon.science.rocrate import export_rocrate
from opencodon.science.runtime import contains_magics


# ── traceback → line number ─────────────────────────────────────────


@pytest.mark.parametrize(
    "frames,expected",
    [
        (("Traceback", "Cell In[3], line 2", "ValueError: nope"), 2),
        (('File "<ipython-input-3-abc>", line 7, in <module>',), 7),
        ((r'File /tmp/ipykernel_11779/482.py, line 12, in <module>',), 12),
        # Deepest cell frame wins — a cell that calls its own helper reports
        # where the failure happened, not where the call started.
        (("Cell In[1], line 4", "Cell In[1], line 9"), 9),
        # Library frames are not cell frames.
        (('File "/usr/lib/python3.11/json/decoder.py", line 355',), None),
        ((), None),
        (None, None),
    ],
)
@pytest.mark.requirement("SCI-P0-01")
def test_error_lineno_reads_only_cell_frames(frames, expected):
    assert error_lineno_from_traceback(frames) == expected


@pytest.mark.requirement("SCI-P0-01")
def test_error_lineno_survives_ansi_colouring():
    """Real kernels colour tracebacks; the number must still come through."""
    coloured = ("\x1b[0;32mCell In[2], line 5\x1b[0m",)
    assert error_lineno_from_traceback(coloured) == 5


@pytest.mark.requirement("SCI-P0-02")
def test_traceback_text_strips_ansi_and_joins():
    text = traceback_text(("\x1b[31mTraceback\x1b[0m", "Cell In[1], line 1"))
    assert text == "Traceback\nCell In[1], line 1"
    assert "\x1b" not in text


@pytest.mark.requirement("SCI-P0-03")
def test_traceback_text_empty_is_none():
    assert traceback_text(()) is None
    assert traceback_text(None) is None


# ── magic detection ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "source,expected",
    [
        ("%timeit f()", True),
        ("%%bash\nls -la", True),
        ("!ls -la", True),
        ("import os\n%matplotlib inline", True),
        ("x = 1\nprint(x)", False),
        # `!=` is a comparison, not a shell escape.
        ("assert a != b", False),
        # An indented `%` is a modulo continuation, not a magic.
        ("x = (a\n     %b)", False),
        ("", False),
    ],
)
@pytest.mark.requirement("SCI-P0-06")
def test_contains_magics(source, expected):
    assert contains_magics(source) is expected


# ── recorded on the cell row ────────────────────────────────────────


@pytest.mark.requirement("SCI-P0-01", "SCI-P0-02")
def test_failing_cell_records_location(science_runtime):
    result = science_runtime.run_cell(
        "s1", "x = 1\ny = 2\nraise ValueError('nope')\n"
    )
    assert result["status"] == "error"
    # The model still sees only name/value — the frames stay out of context.
    assert result["error"] == {"name": "ValueError", "value": "nope"}
    assert "traceback" not in result

    row = science_runtime.store.get_cell(result["cell_id"])
    assert row["error_lineno"] == 3
    assert "ValueError: nope" in row["traceback"]


@pytest.mark.requirement("SCI-P0-03")
def test_successful_cell_has_no_failure_evidence(science_runtime):
    result = science_runtime.run_cell("s1", "print('ok')")
    row = science_runtime.store.get_cell(result["cell_id"])
    assert row["exit_status"] == "ok"
    assert row["error_lineno"] is None
    assert row["traceback"] is None


@pytest.mark.requirement("SCI-P0-06")
def test_magics_flagged_on_the_row(science_runtime):
    plain = science_runtime.run_cell("s1", "x = 1")
    assert science_runtime.store.get_cell(plain["cell_id"])["has_magics"] == 0

    # Recorded even though this kernel double cannot execute it — the flag
    # describes the source, not the outcome.
    magic = science_runtime.run_cell("s1", "%timeit x = 1")
    assert science_runtime.store.get_cell(magic["cell_id"])["has_magics"] == 1


# ── dropped display output ──────────────────────────────────────────


@pytest.mark.requirement("SCI-P0-04")
def test_unsaved_display_is_counted_and_surfaced(displaying_runtime):
    result = displaying_runtime.run_cell("s1", "print('plotted')")

    assert result["unsaved_displays"] == 1
    assert "save_artifact" in result["note"]
    # The payload itself never reaches the model.
    assert "b64…" not in json.dumps(result)

    row = displaying_runtime.store.get_cell(result["cell_id"])
    assert row["display_count"] == 1


@pytest.mark.requirement("SCI-P0-05")
def test_no_nag_when_the_cell_saved_something(displaying_runtime):
    result = displaying_runtime.run_cell(
        "s1", "save_artifact('hello', 'out.txt')"
    )
    assert result["artifacts"]
    assert result["unsaved_displays"] == 1
    # A figure still went uncaptured, so the count stands — but the cell did
    # save, so it does not get told how to.
    assert "note" not in result


@pytest.mark.requirement("SCI-P0-04")
def test_cell_without_display_reports_nothing(science_runtime):
    result = science_runtime.run_cell("s1", "x = 1")
    assert "unsaved_displays" not in result
    assert science_runtime.store.get_cell(result["cell_id"])["display_count"] == 0


# ── RO-Crate portability caveat ─────────────────────────────────────


def _actions(crate_path: Path) -> list:
    graph = json.loads(crate_path.read_text())["@graph"]
    return [e for e in graph if e.get("@type") == "CreateAction"]


@pytest.mark.requirement("SCI-P0-07")
def test_rocrate_flags_non_replayable_source(science_runtime, tmp_path):
    # The flag is set here rather than by running a magic, because only a real
    # IPython kernel can execute one *and* still produce an artifact — and a
    # cell that produces nothing never enters the crate. Detection itself is
    # covered by test_contains_magics / test_magics_flagged_on_the_row; what
    # matters here is what the exporter does with the column.
    magic = science_runtime.run_cell("s1", "save_artifact('x', 'magic.txt')")
    science_runtime.run_cell("s1", "save_artifact('y', 'plain.txt')")
    science_runtime.store.update_cell(magic["cell_id"], has_magics=1)

    crate = export_rocrate(
        science_runtime.root_for("s1"), tmp_path / "crate", runtime=science_runtime
    )
    actions = _actions(crate)
    assert actions, "expected producing cells in the crate"

    caveats = [a.get("disambiguatingDescription") for a in actions]
    flagged = [c for c in caveats if c]
    assert len(flagged) == 1, "only the magic cell should carry the caveat"
    assert "not plain python" in flagged[0].lower()
