"""Biomodel skills — attribution, hardware honesty, and one real CPU run.

These skills wrap other people's models. The licence is the entire basis for
using them, so attribution is checked mechanically rather than trusted, and a
model whose upstream terms we could not establish is checked to be *absent*.

Only ProteinMPNN is executed for real. It is the one the upstream documents as
CPU-capable, and it runs here in well under a second. The GPU models are
imported and attributed but deliberately unvalidated — see the note in
tests/requirements.yaml.
"""

import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
SKILLS_ROOT = REPO / "skills" / "science"

MPNN_REPO = "https://github.com/dauparas/ProteinMPNN.git"


def frontmatter(skill_dir: Path) -> dict:
    text = (skill_dir / "SKILL.md").read_text()
    match = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    assert match, f"{skill_dir.name} has no frontmatter"
    return yaml.safe_load(match.group(1))


def biomodel_skills():
    found = []
    for path in sorted(SKILLS_ROOT.iterdir()):
        if not path.is_dir():
            continue
        meta = frontmatter(path)
        if "biomodels" in (meta.get("metadata", {}).get("opencodon", {}).get("tags") or []):
            found.append(path)
    return found


BIOMODELS = biomodel_skills() if SKILLS_ROOT.is_dir() else []
BIOMODEL_IDS = [p.name for p in BIOMODELS]

# Documented by upstream as CPU-capable — the MPNN family is a small
# message-passing net, not a transformer stack.
CPU_CAPABLE = {"proteinmpnn", "solublempnn", "ligandmpnn"}


def test_biomodels_were_imported():
    assert len(BIOMODELS) >= 10, f"expected the biomodel set, found {BIOMODEL_IDS}"


# ── SCI-P4-01 attribution ───────────────────────────────────────────


@pytest.mark.requirement("SCI-P4-01")
@pytest.mark.parametrize("skill_dir", BIOMODELS, ids=BIOMODEL_IDS)
def test_weights_are_attributed(skill_dir):
    meta = frontmatter(skill_dir)
    entries = meta["metadata"].get("third_party") or []

    if skill_dir.name == "scvi-tools":
        # A pip-installable library rather than a weights download; it has no
        # third_party block upstream and inventing one would be fabrication.
        pytest.skip("scvi-tools ships no third_party block upstream")

    weights = [e for e in entries if e.get("kind") == "weights"]
    assert weights, f"{skill_dir.name} names no weights"
    for entry in weights:
        assert entry.get("name")
        assert entry.get("license"), f"{skill_dir.name}: {entry['name']} has no licence"
        assert entry.get("terms_url"), f"{skill_dir.name}: {entry['name']} has no terms URL"


@pytest.mark.requirement("SCI-P4-01")
@pytest.mark.parametrize("skill_dir", BIOMODELS, ids=BIOMODEL_IDS)
def test_provenance_is_recorded(skill_dir):
    provenance = frontmatter(skill_dir)["metadata"]["provenance"]
    assert provenance["upstream_license"] == "Apache-2.0"
    assert len(provenance["modifications"].strip()) > 20


# ── SCI-P4-02 hardware honesty ──────────────────────────────────────


@pytest.mark.requirement("SCI-P4-02")
@pytest.mark.parametrize("skill_dir", BIOMODELS, ids=BIOMODEL_IDS)
def test_gpu_requirement_matches_reality(skill_dir):
    declared = frontmatter(skill_dir).get("requirements") or []
    needs_gpu = "gpu" in declared
    if skill_dir.name in CPU_CAPABLE:
        # Claiming a GPU it does not need would send work to a rented
        # accelerator for something that finishes locally in under a second.
        assert not needs_gpu, f"{skill_dir.name} is CPU-capable but demands a GPU"
    else:
        assert needs_gpu, f"{skill_dir.name} needs a GPU but does not say so"


# ── SCI-P4-03 unresolved licensing ──────────────────────────────────


@pytest.mark.requirement("SCI-P4-03")
@pytest.mark.parametrize("name", ["borzoi", "scgpt"])
def test_models_with_unresolved_terms_are_absent(name):
    """borzoi declares no terms URL upstream; scgpt declares no licence.

    Shipping either would mean asserting an attribution nobody verified.
    """
    assert not (SKILLS_ROOT / name).exists(), (
        f"{name} was imported before its upstream terms were established"
    )


# ── SCI-P4-04 somewhere to run ──────────────────────────────────────


@pytest.mark.requirement("SCI-P4-04")
def test_a_gpu_target_can_be_produced():
    from science.provisioners import get_provisioner

    provisioner = get_provisioner("modal", gpu="A100")
    assert provisioner.gpu == "A100"
    # The accelerator lands in the provenance, so a structure predicted on an
    # A100 is distinguishable from one predicted anywhere else.
    assert provisioner.describe_target().endswith("/A100")


# ── SCI-P4-10 a real CPU inference ──────────────────────────────────


def _write_backbone(path: Path, residues: int = 12) -> None:
    """A minimal extended poly-alanine backbone — enough geometry to design on."""
    atoms = {"N": (0.0, 0.0, 0.0), "CA": (1.458, 0.0, 0.0),
             "C": (2.009, 1.42, 0.0), "O": (1.251, 2.39, 0.0)}
    lines, serial = [], 1
    for i in range(1, residues + 1):
        dx = 3.8 * (i - 1)
        for atom, (x, y, z) in atoms.items():
            lines.append(
                f"ATOM  {serial:5d}  {atom:<3s} ALA A{i:4d}    "
                f"{x + dx:8.3f}{y:8.3f}{z:8.3f}  1.00  0.00           {atom[0]}"
            )
            serial += 1
    path.write_text("\n".join(lines + ["TER", "END"]) + "\n")


@pytest.mark.integration
@pytest.mark.requirement("SCI-P4-10")
def test_proteinmpnn_designs_sequences_on_cpu(science_runtime, tmp_path):
    """Real weights, real inference, no GPU — through run_code.

    torch is deliberately not a dependency of this repo (it was cut with the
    ML machinery), so the cell runs the model in an ephemeral `uv --with`
    environment. That is exactly what the skill's "pip install torch if not
    already present" means for an install that does not carry it, and it
    leaves nothing behind.
    """
    if not (SKILLS_ROOT / "proteinmpnn").exists():
        pytest.skip("proteinmpnn is not installed")

    checkout = tmp_path / "ProteinMPNN"
    clone = subprocess.run(
        ["git", "clone", "--depth", "1", "-q", MPNN_REPO, str(checkout)],
        capture_output=True, text=True, timeout=600,
    )
    if clone.returncode != 0:
        pytest.skip(f"could not clone ProteinMPNN: {clone.stderr[:200]}")

    backbone = tmp_path / "backbone.pdb"
    _write_backbone(backbone)

    result = science_runtime.run_cell(
        "mpnn",
        "import subprocess, pathlib\n"
        f"out = pathlib.Path({str(tmp_path / 'out')!r})\n"
        "proc = subprocess.run(\n"
        "    ['uv', 'run', '--with', 'torch', '--with', 'numpy', 'python',\n"
        f"     {str(checkout / 'protein_mpnn_run.py')!r},\n"
        f"     '--pdb_path', {str(backbone)!r}, '--pdb_path_chains', 'A',\n"
        "     '--out_folder', str(out), '--num_seq_per_target', '4',\n"
        "     '--sampling_temp', '0.1'],\n"
        "    capture_output=True, text=True, timeout=900)\n"
        "print('rc', proc.returncode)\n"
        "fasta = next(out.glob('seqs/*.fa'))\n"
        "designs = fasta.read_text()\n"
        "print(designs)\n"
        "save_artifact(designs, 'designs.fa')\n",
        timeout=1200,
    )

    assert result["status"] == "ok", result.get("error")
    assert "rc 0" in result["stdout"], result["stdout"][-800:]
    # The model reports per-design scores and recovery against the input.
    assert "score=" in result["stdout"]
    assert "seq_recovery=" in result["stdout"]

    [artifact] = result["artifacts"]
    designs = science_runtime.blobs.read_bytes(artifact["sha256"]).decode()
    sequences = [
        line.strip() for line in designs.splitlines()
        if line.strip() and not line.startswith(">")
    ]
    # One input sequence plus four designs, each the length of the backbone.
    assert len(sequences) == 5
    assert all(len(seq) == 12 for seq in sequences), sequences
    assert set("".join(sequences)) <= set("ACDEFGHIKLMNPQRSTVWY")
