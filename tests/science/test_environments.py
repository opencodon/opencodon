"""Durable environments and the reproducibility claim they unlock.

The point of this phase is not package management. It is that
``execution_log.env_snapshot`` was an *observation* — a list of what happened
to be installed, which cannot rebuild anything — and ``reproduce()`` was
correspondingly capped at "the bytes matched". A lockfile identity turns that
into a recipe, and the claim can become "verified".

So the tests that matter most here are the ones about what is *not* an
identity, and about the grading that depends on it.
"""

import json

import pytest

from science import envmanager as em
from science.envmanager import EnvError


# ── SCI-P6-01 durability ────────────────────────────────────────────


@pytest.mark.requirement("SCI-P6-01")
def test_environments_live_outside_any_session_workspace():
    """Workspaces are swept; an environment that went with one is not durable."""
    root = em.root_prefix()
    assert root.name == "science-envs"
    assert "workspace" not in str(root)
    assert em.env_prefix("demo") == root / "envs" / "demo"


@pytest.mark.requirement("SCI-P6-01")
def test_unknown_environment_operations_are_named_errors():
    with pytest.raises(EnvError):
        em.export_lock("no-such-env")
    with pytest.raises(EnvError):
        em.install("no-such-env", ["numpy"])
    with pytest.raises(EnvError):
        em.remove("no-such-env")
    with pytest.raises(EnvError):
        em.create("", ["numpy"])


# ── SCI-P6-02 identity ──────────────────────────────────────────────


LOCK = """# This file may be used to create an environment using:
# platform: osx-arm64
@EXPLICIT
https://conda.anaconda.org/conda-forge/osx-arm64/python-3.11.15-h1.conda#aaa
https://conda.anaconda.org/conda-forge/osx-arm64/numpy-2.4.6-h2.conda#bbb
"""


@pytest.mark.requirement("SCI-P6-02")
def test_lock_hash_ignores_comments_and_ordering():
    """The same environment exported twice must hash the same.

    micromamba writes a platform comment and does not promise a stable order;
    neither is part of what makes two environments the same.
    """
    reordered = "\n".join(reversed(LOCK.splitlines()))
    commented = LOCK.replace("# platform: osx-arm64", "# platform: osx-arm64 (rebuilt)")

    assert em.lock_hash(LOCK) == em.lock_hash(reordered)
    assert em.lock_hash(LOCK) == em.lock_hash(commented)


@pytest.mark.requirement("SCI-P6-02")
def test_lock_hash_changes_when_a_package_does():
    upgraded = LOCK.replace("numpy-2.4.6", "numpy-2.5.0")
    assert em.lock_hash(LOCK) != em.lock_hash(upgraded)


@pytest.mark.requirement("SCI-P6-02")
def test_snapshot_carries_both_the_identity_and_the_recipe(monkeypatch):
    monkeypatch.setattr(em, "exists", lambda name: True)
    monkeypatch.setattr(em, "export_lock", lambda name: LOCK)

    payload = json.loads(em.env_snapshot("demo"))
    assert payload["manager"] == "micromamba"
    # The hash answers "is this the same environment"; the lock answers
    # "then build me one".
    assert payload["lock_hash"] == em.lock_hash(LOCK)
    assert "@EXPLICIT" in payload["lock"]


# ── SCI-P6-03 an observation is not a recipe ────────────────────────


@pytest.mark.requirement("SCI-P6-03")
@pytest.mark.parametrize("snapshot", [
    None,
    "",
    # The pre-micromamba shape: what was installed, with no way to rebuild it.
    '{"language": "python", "python_version": "3.11.9", "distributions": ["numpy==2.0"]}',
    "not json at all",
    "[1, 2, 3]",
])
def test_observational_snapshots_yield_no_identity(snapshot):
    assert em.snapshot_lock_hash(snapshot) is None


@pytest.mark.requirement("SCI-P6-03")
def test_a_real_snapshot_yields_its_identity():
    snapshot = json.dumps({"manager": "micromamba", "lock_hash": "abc123"})
    assert em.snapshot_lock_hash(snapshot) == "abc123"


# ── SCI-P6-05 recorded per cell ─────────────────────────────────────


@pytest.mark.requirement("SCI-P6-05")
def test_every_cell_records_the_identity_not_just_the_first(science_runtime, monkeypatch):
    """The producing cell is rarely the first cell of its kernel.

    env_snapshot is written once per kernel because it is bulky; the hash has
    to be on every row or reproduce() has nothing to compare.
    """
    monkeypatch.setattr(
        "science.runtime._lock_hash_of", lambda snapshot: "deadbeef" if snapshot else None
    )
    first = science_runtime.run_cell("s1", "x = 1")
    second = science_runtime.run_cell("s1", "y = 2")

    rows = [science_runtime.store.get_cell(r["cell_id"]) for r in (first, second)]
    assert rows[0]["env_lock_hash"] == "deadbeef"
    assert rows[1]["env_lock_hash"] == "deadbeef"
    # The bulky snapshot itself stays on the first cell only.
    assert rows[0]["env_snapshot"] and not rows[1]["env_snapshot"]


@pytest.mark.requirement("SCI-P6-05")
def test_observational_environments_record_no_identity(science_runtime):
    """The default local kernel has no lock, so the column stays NULL."""
    result = science_runtime.run_cell("s1", "x = 1")
    assert science_runtime.store.get_cell(result["cell_id"])["env_lock_hash"] is None


# ── SCI-P6-06 the grading that depends on it ────────────────────────


@pytest.mark.requirement("SCI-P6-06")
def test_byte_match_without_an_identity_is_only_reproduced(science_runtime):
    from science.reproduce import reproduce

    result = science_runtime.run_cell("s1", "save_artifact('stable', 'out.txt')")
    [artifact] = result["artifacts"]

    report = reproduce(artifact["version_id"], runtime=science_runtime)
    assert report["claim"] == "reproduced"
    # And it says why it is not more than that.
    assert any("observation-only" in c for c in report["caveats"])


@pytest.mark.requirement("SCI-P6-06")
def test_byte_match_under_a_matching_lock_is_verified(science_runtime, monkeypatch):
    from science.reproduce import reproduce

    monkeypatch.setattr(
        "science.runtime._lock_hash_of", lambda snapshot: "lock-aaa" if snapshot else None
    )
    result = science_runtime.run_cell("s1", "save_artifact('stable', 'out.txt')")
    [artifact] = result["artifacts"]

    report = reproduce(artifact["version_id"], runtime=science_runtime)
    assert report["claim"] == "verified"
    assert "lock" in report["reason"]
    assert not any("observation-only" in c for c in report["caveats"])


@pytest.mark.requirement("SCI-P6-06")
def test_a_changed_environment_downgrades_to_reproduced(science_runtime, monkeypatch):
    """Bytes can match under a different environment — that is luck, not proof."""
    from science.reproduce import reproduce

    locks = iter(["lock-aaa"] + ["lock-bbb"] * 10)
    monkeypatch.setattr(
        "science.runtime._lock_hash_of",
        lambda snapshot: next(locks) if snapshot else None,
    )
    result = science_runtime.run_cell("s1", "save_artifact('stable', 'out.txt')")
    [artifact] = result["artifacts"]

    report = reproduce(artifact["version_id"], runtime=science_runtime)
    assert report["claim"] == "reproduced"
    assert any("differs from the recorded" in c for c in report["caveats"])


# ── tool surface ────────────────────────────────────────────────────


@pytest.mark.requirement("SCI-P6-01")
def test_environments_toolset_matches_the_registry():
    import tools.env_tools  # noqa: F401 - registers on import
    from toolsets import TOOLSETS
    from tools.registry import registry

    declared = set(TOOLSETS["environments"]["tools"])
    registered = {
        name for name, entry in registry._tools.items() if entry.toolset == "environments"
    }
    assert declared == registered


@pytest.mark.requirement("SCI-P6-01")
def test_env_errors_come_back_as_data():
    import tools.env_tools as env_tools

    payload = json.loads(env_tools._call(em.export_lock, name="no-such-env"))
    assert payload["source"] == "micromamba"
    assert "does not exist" in payload["error"]


# ── live micromamba ─────────────────────────────────────────────────


@pytest.fixture
def live_env():
    """A tiny real environment, removed afterwards."""
    name = "oc-test-env"
    if em.exists(name):
        em.remove(name)
    em.create(name, ["numpy"], python="3.11")
    yield name
    if em.exists(name):
        em.remove(name)


@pytest.mark.integration
@pytest.mark.requirement("SCI-P6-01")
@pytest.mark.requirement("SCI-P6-02")
def test_live_environment_is_durable_and_identified(live_env):
    assert live_env in em.list_envs()
    spec = em.describe(live_env)

    assert spec.python.exists()
    assert len(spec.lock_hash) == 64
    # Exporting twice must give the same identity, or nothing downstream can
    # compare environments at all.
    assert em.describe(live_env).lock_hash == spec.lock_hash


@pytest.mark.integration
@pytest.mark.requirement("SCI-P6-04")
@pytest.mark.requirement("SCI-P6-05")
def test_live_kernel_runs_inside_the_environment(db, tmp_path, live_env):
    from science.blobstore import BlobStore
    from science.bridge import bootstrap_kernel
    from science.host_bridge import shutdown_bridges
    from science.kernels import SessionKernelManager, kernels_installed
    from science.runtime import ScienceRuntime

    if not kernels_installed():
        pytest.skip("jupyter kernel stack not installed")

    db.create_session("envs", source="cli")
    manager = SessionKernelManager(
        workspaces_root=tmp_path / "ws", bootstrap_fn=bootstrap_kernel
    )
    runtime = ScienceRuntime(db, blobs=BlobStore(tmp_path / "blobs"), manager=manager)
    try:
        result = runtime.run_cell(
            "envs",
            "import numpy, sys\nprint(numpy.__version__)\nprint(sys.prefix)",
            env=live_env, timeout=180,
        )
        assert result["status"] == "ok", result.get("error")
        # It really is the environment's interpreter, not ours.
        assert live_env in result["stdout"]

        row = runtime.store.get_cell(result["cell_id"])
        assert row["env_name"] == f"micromamba:{live_env}"
        assert row["env_lock_hash"] == em.describe(live_env).lock_hash
    finally:
        shutdown_bridges()
        manager.shutdown()
