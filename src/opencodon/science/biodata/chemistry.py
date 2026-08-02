"""Chemistry — PubChem structures and ChEMBL bioactivity.

The two sit at different ends of the same question. PubChem is the structural
registry: given a name or a SMILES, what is this molecule. ChEMBL is the
pharmacology: what has it been measured to *do*, against which target, at what
potency, in whose hands.

A note on PubChem's property names: the SMILES field is ``ConnectivitySMILES``
in the current API, and code written against the older ``CanonicalSMILES`` gets
back nothing rather than an error. Both are requested and coalesced.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from opencodon.science.apiclient import ApiClient, ApiError, bounded_count, clip

PUBCHEM_URL = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"
CHEMBL_URL = "https://www.ebi.ac.uk/chembl/api/data"

SOURCE_PUBCHEM = "pubchem"
SOURCE_CHEMBL = "chembl"

PROPERTIES = (
    "MolecularFormula,MolecularWeight,CanonicalSMILES,ConnectivitySMILES,"
    "IsomericSMILES,InChIKey,XLogP,TPSA,HBondDonorCount,HBondAcceptorCount"
)


def _pubchem(**kwargs) -> ApiClient:
    return ApiClient(SOURCE_PUBCHEM, PUBCHEM_URL, **kwargs)


def _shape_compound(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "cid": row.get("CID"),
        "formula": row.get("MolecularFormula"),
        "molecular_weight": row.get("MolecularWeight"),
        # The API renamed this field; accept either so a rename upstream
        # degrades to the other rather than to silence.
        "smiles": (
            row.get("ConnectivitySMILES")
            or row.get("CanonicalSMILES")
            or row.get("IsomericSMILES")
        ),
        "isomeric_smiles": row.get("IsomericSMILES"),
        "inchikey": row.get("InChIKey"),
        "xlogp": row.get("XLogP"),
        "tpsa": row.get("TPSA"),
        "hbond_donors": row.get("HBondDonorCount"),
        "hbond_acceptors": row.get("HBondAcceptorCount"),
    }


def compound(name: str, *, namespace: str = "name", **kwargs) -> Dict[str, Any]:
    """Structure and physicochemical properties for a compound.

    *namespace* is PubChem's lookup scheme — ``name``, ``cid``, ``smiles`` or
    ``inchikey``.
    """
    if not (name or "").strip():
        raise ApiError(SOURCE_PUBCHEM, "a compound name or identifier is required")
    allowed = {"name", "cid", "smiles", "inchikey"}
    if namespace not in allowed:
        raise ApiError(
            SOURCE_PUBCHEM, f"namespace must be one of {', '.join(sorted(allowed))}"
        )
    payload = _pubchem(**kwargs).get_json(
        f"/compound/{namespace}/{name.strip()}/property/{PROPERTIES}/JSON"
    )
    rows = ((payload.get("PropertyTable") or {}).get("Properties")) or []
    if not rows:
        raise ApiError(SOURCE_PUBCHEM, f"no PubChem compound for {name!r}", status=404)
    return {"source": SOURCE_PUBCHEM, **_shape_compound(rows[0])}


def similar_compounds(
    smiles: str, *, threshold: int = 90, limit: Optional[int] = None, **kwargs
) -> Dict[str, Any]:
    """2D-similarity search over PubChem by Tanimoto threshold."""
    if not (smiles or "").strip():
        raise ApiError(SOURCE_PUBCHEM, "a SMILES string is required")
    if not 0 < int(threshold) <= 100:
        raise ApiError(SOURCE_PUBCHEM, "threshold must be a percentage in 1..100")
    count = bounded_count(limit)
    payload = _pubchem(**kwargs).get_json(
        f"/compound/fastsimilarity_2d/smiles/{smiles.strip()}/property/{PROPERTIES}/JSON",
        {"Threshold": int(threshold), "MaxRecords": count},
    )
    rows = ((payload.get("PropertyTable") or {}).get("Properties")) or []
    # Slice before counting: PubChem can return more than MaxRecords asked for,
    # and a `returned` that describes the upstream page rather than this
    # payload is a number the caller cannot act on.
    page = rows[:count]
    return {
        "source": SOURCE_PUBCHEM,
        "query": smiles,
        "threshold": int(threshold),
        "matched": len(rows),
        "returned": len(page),
        "results": [_shape_compound(row) for row in page],
    }


# ── ChEMBL ──────────────────────────────────────────────────────────


def _chembl(**kwargs) -> ApiClient:
    return ApiClient(SOURCE_CHEMBL, CHEMBL_URL, **kwargs)


def drug_search(query: str, *, limit: Optional[int] = None, **kwargs) -> Dict[str, Any]:
    """Search ChEMBL molecules by name or synonym."""
    if not (query or "").strip():
        raise ApiError(SOURCE_CHEMBL, "a search query is required")
    count = bounded_count(limit)
    payload = _chembl(**kwargs).get_json(
        "/molecule/search", {"q": query.strip(), "format": "json", "limit": count}
    )
    molecules = payload.get("molecules") or []
    results = []
    for molecule in molecules[:count]:
        structures = molecule.get("molecule_structures") or {}
        properties = molecule.get("molecule_properties") or {}
        results.append({
            "chembl_id": molecule.get("molecule_chembl_id"),
            "name": molecule.get("pref_name"),
            "max_phase": molecule.get("max_phase"),
            "molecule_type": molecule.get("molecule_type"),
            "smiles": structures.get("canonical_smiles"),
            "inchikey": structures.get("standard_inchi_key"),
            "molecular_weight": properties.get("full_mwt"),
            "alogp": properties.get("alogp"),
            # A boxed warning is the kind of fact a reader should not have to
            # go looking for.
            "black_box_warning": bool(molecule.get("black_box_warning")),
            "atc_codes": molecule.get("atc_classifications") or [],
        })
    return {
        "source": SOURCE_CHEMBL,
        "query": query,
        "returned": len(results),
        "results": results,
    }


def bioactivities(
    chembl_id: str, *, limit: Optional[int] = None, **kwargs
) -> Dict[str, Any]:
    """Measured activities for a ChEMBL molecule."""
    if not (chembl_id or "").strip():
        raise ApiError(SOURCE_CHEMBL, "a ChEMBL molecule id is required")
    count = bounded_count(limit)
    payload = _chembl(**kwargs).get_json(
        "/activity",
        {"molecule_chembl_id": chembl_id.strip(), "format": "json", "limit": count},
    )
    activities: List[Dict[str, Any]] = []
    for entry in (payload.get("activities") or [])[:count]:
        activities.append({
            "target_chembl_id": entry.get("target_chembl_id"),
            "target_name": entry.get("target_pref_name"),
            "organism": entry.get("target_organism"),
            "type": entry.get("standard_type"),
            "relation": entry.get("standard_relation"),
            "value": entry.get("standard_value"),
            "units": entry.get("standard_units"),
            "pchembl": entry.get("pchembl_value"),
            "assay_description": clip(entry.get("assay_description"), 300),
            # The source document is what makes a number checkable.
            "document_chembl_id": entry.get("document_chembl_id"),
        })
    total = (payload.get("page_meta") or {}).get("total_count")
    return {
        "source": SOURCE_CHEMBL,
        "molecule": chembl_id,
        "total": total,
        "returned": len(activities),
        "results": activities,
    }
