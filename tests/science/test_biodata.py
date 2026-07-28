"""Bio-data layer — shaping, bounding, and the claims that must not blur.

Stubbed by default for the same reason as the literature tests: a test that
needs gnomAD to be up tells you about gnomAD. The live tests at the bottom are
integration-marked.

One of these is not really a shaping test. ClinVar's review status is what
separates "one submitter said pathogenic" from "an expert panel reviewed it",
and a payload that drops it makes those read identically — so it is pinned.
"""

import json

import httpx
import pytest

from science.apiclient import MAX_RESULTS, ApiError
from science.biodata import chemistry, genes, variants


class StubTransport(httpx.BaseTransport):
    """Canned responses, consumed in order; the last repeats."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def handle_request(self, request):
        self.requests.append(request)
        spec = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        status, body = spec
        content = body if isinstance(body, bytes) else json.dumps(body).encode()
        return httpx.Response(
            status, content=content,
            headers={"Content-Type": "application/json"}, request=request,
        )


def ok(payload):
    return (200, payload)


# ── SCI-P5-01 / 02 gene identity ────────────────────────────────────


@pytest.mark.requirement("SCI-P5-01")
def test_gene_lookup_shapes_coordinates():
    transport = StubTransport(ok({
        "id": "ENSG00000012048", "display_name": "BRCA1",
        "description": "BRCA1 DNA repair associated", "biotype": "protein_coding",
        "assembly_name": "GRCh38", "seq_region_name": "17",
        "start": 43044295, "end": 43170245, "strand": -1,
        "canonical_transcript": "ENST00000357654.9",
    }))
    result = genes.gene_lookup("BRCA1", transport=transport)

    assert result["gene_id"] == "ENSG00000012048"
    assert result["chromosome"] == "17"
    assert result["strand"] == -1
    assert result["assembly"] == "GRCh38"


@pytest.mark.requirement("SCI-P5-01")
def test_gene_lookup_requires_a_symbol():
    with pytest.raises(ApiError):
        genes.gene_lookup("")


@pytest.mark.requirement("SCI-P5-02")
def test_resolve_gene_returns_every_scheme():
    transport = StubTransport(ok({
        "total": 45,
        "hits": [{
            "symbol": "BRCA1", "name": "BRCA1 DNA repair associated",
            "entrezgene": "672", "ensembl": {"gene": "ENSG00000012048"},
            "uniprot": {"Swiss-Prot": "P38398"}, "taxid": 9606, "_score": 145.1,
        }],
    }))
    [hit] = genes.resolve_gene("BRCA1", transport=transport)["results"]

    # All three schemes together — that is the whole point of the call.
    assert hit["entrez_id"] == "672"
    assert hit["ensembl_id"] == "ENSG00000012048"
    assert hit["uniprot_id"] == "P38398"


@pytest.mark.requirement("SCI-P5-02")
def test_multi_mapping_genes_do_not_crash():
    """Genes on patch scaffolds come back with ensembl as a list, not a dict."""
    transport = StubTransport(ok({
        "total": 1,
        "hits": [{
            "symbol": "X", "ensembl": [{"gene": "ENSG1"}, {"gene": "ENSG2"}],
            "entrezgene": "1",
        }],
    }))
    [hit] = genes.resolve_gene("X", transport=transport)["results"]
    assert hit["ensembl_id"] == "ENSG1"


# ── SCI-P5-03 bounded sequence ──────────────────────────────────────


@pytest.mark.requirement("SCI-P5-03")
def test_long_sequence_is_previewed_with_its_true_length():
    transport = StubTransport(ok({"id": "ENSG1", "molecule": "dna", "seq": "A" * 50_000}))
    result = genes.sequence("ENSG1", transport=transport)

    assert result["length"] == 50_000
    assert result["truncated"] is True
    assert len(result["sequence_preview"]) < 2000


@pytest.mark.requirement("SCI-P5-03")
def test_short_sequence_is_not_marked_truncated():
    transport = StubTransport(ok({"id": "ENSG1", "molecule": "dna", "seq": "ACGT"}))
    result = genes.sequence("ENSG1", transport=transport)
    assert result["truncated"] is False
    assert result["sequence_preview"] == "ACGT"


# ── SCI-P5-04 protein ───────────────────────────────────────────────


@pytest.mark.requirement("SCI-P5-04")
def test_protein_record_is_shaped():
    transport = StubTransport(ok({
        "primaryAccession": "P38398", "uniProtkbId": "BRCA1_HUMAN",
        "proteinDescription": {"recommendedName": {"fullName": {"value": "BRCA1"}}},
        "genes": [{"geneName": {"value": "BRCA1"}}],
        "organism": {"scientificName": "Homo sapiens"},
        "sequence": {"value": "MDLSALRVEE"},
        "comments": [{"commentType": "FUNCTION",
                      "texts": [{"value": "E3 ubiquitin-protein ligase."}]}],
    }))
    result = genes.protein("P38398", transport=transport)

    assert result["accession"] == "P38398"
    assert result["organism"] == "Homo sapiens"
    assert result["genes"] == ["BRCA1"]
    assert "ubiquitin" in result["function"]


# ── SCI-P5-10 / 11 variants ─────────────────────────────────────────


@pytest.mark.requirement("SCI-P5-10")
def test_variant_frequency_breaks_down_by_ancestry():
    transport = StubTransport(ok({"data": {"variant": {
        "variant_id": "1-55039841-G-T", "rsids": ["rs1"],
        "exome": {"ac": 3, "an": 100, "af": 0.03,
                  "populations": [{"id": "afr", "ac": 3, "an": 50}]},
        "genome": None,
    }}}))
    result = variants.variant_frequency("1-55039841-G-T", transport=transport)

    assert result["exome"]["allele_frequency"] == 0.03
    # A variant rare overall can be common in one population; that is the
    # number that changes an interpretation.
    [population] = result["exome"]["populations"]
    assert population["population"] == "afr"
    assert population["allele_frequency"] == pytest.approx(0.06)
    assert result["genome"] is None


@pytest.mark.requirement("SCI-P5-10")
def test_missing_variant_is_a_named_error():
    transport = StubTransport(ok({"data": {"variant": None}}))
    with pytest.raises(ApiError) as caught:
        variants.variant_frequency("9-9-A-T", transport=transport)
    assert caught.value.status == 404


@pytest.mark.requirement("SCI-P5-11")
def test_gene_variants_filter_reports_what_it_left_behind():
    payload = {"data": {"gene": {"gene_id": "ENSG1", "variants": [
        {"variant_id": "1-1-A-T", "consequence": "missense_variant",
         "genome": {"af": 0.1}, "exome": None},
        {"variant_id": "1-2-A-T", "consequence": "synonymous_variant",
         "genome": None, "exome": None},
        {"variant_id": "1-3-A-T", "consequence": "missense_variant",
         "genome": None, "exome": {"af": 0.2}},
    ]}}}
    result = variants.gene_variants(
        "PCSK9", consequence="missense_variant", limit=1,
        transport=StubTransport(ok(payload)),
    )
    # Two matched the filter, one was returned — both numbers are visible, so
    # truncation cannot be mistaken for the whole answer.
    assert result["total"] == 2
    assert result["returned"] == 1


@pytest.mark.requirement("SCI-P5-11")
def test_unknown_gene_is_a_named_error():
    with pytest.raises(ApiError):
        variants.gene_variants("", transport=StubTransport(ok({})))
    with pytest.raises(ApiError) as caught:
        variants.gene_variants("NOPE", transport=StubTransport(ok({"data": {"gene": None}})))
    assert caught.value.status == 404


@pytest.mark.requirement("SCI-P5-10")
def test_reference_genome_is_validated_locally():
    with pytest.raises(ApiError) as caught:
        variants.gene_region("BRCA1", reference_genome="hg19")
    assert "GRCh38" in str(caught.value)


# ── SCI-P5-12 the claim that must not blur ──────────────────────────


@pytest.mark.requirement("SCI-P5-12")
def test_clinvar_classification_always_carries_review_status():
    transport = StubTransport(ok({"result": {
        "uids": ["1", "2"],
        "1": {"title": "NM_1:c.68del", "accession": "VCV1",
              "genes": [{"symbol": "BRCA1"}], "obj_type": "Deletion",
              "germline_classification": {
                  "description": "Pathogenic",
                  "review_status": "reviewed by expert panel",
                  "last_evaluated": "2024/01/01"}},
        "2": {"title": "NM_1:c.99A>G", "accession": "VCV2", "genes": [],
              "germline_classification": {
                  "description": "Pathogenic",
                  "review_status": "no assertion criteria provided"}},
    }}))
    records = variants.clinvar_records(["1", "2"], transport=transport)["results"]

    # Same word, very different strength of claim. Both must carry the status
    # that distinguishes them.
    assert {r["classification"] for r in records} == {"Pathogenic"}
    assert records[0]["review_status"] == "reviewed by expert panel"
    assert records[1]["review_status"] == "no assertion criteria provided"
    assert all("review_status" in r for r in records)


@pytest.mark.requirement("SCI-P5-12")
def test_clinvar_search_reports_the_unclipped_total():
    transport = StubTransport(ok({
        "esearchresult": {"count": "85110", "idlist": ["1", "2"]}
    }))
    result = variants.clinvar_search("BRCA1", limit=2, transport=transport)
    assert result["total"] == 85110 and result["returned"] == 2


@pytest.mark.requirement("SCI-P5-12")
def test_clinvar_records_require_ids():
    with pytest.raises(ApiError):
        variants.clinvar_records([])


# ── SCI-P5-13 GraphQL errors ────────────────────────────────────────


@pytest.mark.requirement("SCI-P5-13")
def test_graphql_errors_surface_despite_http_200():
    """gnomAD reports failure in the body, so the transport never sees it."""
    transport = StubTransport(ok({"errors": [{"message": "Gene not found"}], "data": None}))
    with pytest.raises(ApiError) as caught:
        variants.gene_region("NOSUCHGENE", transport=transport)
    assert "Gene not found" in str(caught.value)


# ── SCI-P5-20 / 21 / 22 chemistry ───────────────────────────────────


@pytest.mark.requirement("SCI-P5-20")
def test_compound_tolerates_the_smiles_field_rename():
    """PubChem renamed CanonicalSMILES to ConnectivitySMILES; code written for
    the old name gets nothing back rather than an error."""
    modern = StubTransport(ok({"PropertyTable": {"Properties": [
        {"CID": 2244, "MolecularFormula": "C9H8O4",
         "ConnectivitySMILES": "CC(=O)OC1=CC=CC=C1C(=O)O"}]}}))
    assert chemistry.compound("aspirin", transport=modern)["smiles"].startswith("CC(=O)O")

    legacy = StubTransport(ok({"PropertyTable": {"Properties": [
        {"CID": 2244, "CanonicalSMILES": "CC(=O)OC1=CC=CC=C1C(=O)O"}]}}))
    assert chemistry.compound("aspirin", transport=legacy)["smiles"].startswith("CC(=O)O")


@pytest.mark.requirement("SCI-P5-20")
def test_compound_rejects_a_bad_namespace_and_empty_result():
    with pytest.raises(ApiError):
        chemistry.compound("aspirin", namespace="magic")
    empty = StubTransport(ok({"PropertyTable": {"Properties": []}}))
    with pytest.raises(ApiError) as caught:
        chemistry.compound("nothing", transport=empty)
    assert caught.value.status == 404


@pytest.mark.requirement("SCI-P5-21")
def test_similarity_search_bounds_and_validates():
    transport = StubTransport(ok({"PropertyTable": {"Properties": [
        {"CID": i, "ConnectivitySMILES": "C"} for i in range(100)]}}))
    result = chemistry.similar_compounds("CCO", limit=999, transport=transport)
    assert result["returned"] == MAX_RESULTS
    assert transport.requests[0].url.params["MaxRecords"] == str(MAX_RESULTS)

    with pytest.raises(ApiError):
        chemistry.similar_compounds("CCO", threshold=0)


@pytest.mark.requirement("SCI-P5-22")
def test_drug_search_surfaces_safety_flags():
    transport = StubTransport(ok({"molecules": [{
        "molecule_chembl_id": "CHEMBL25", "pref_name": "ASPIRIN", "max_phase": 4,
        "black_box_warning": 1, "atc_classifications": ["N02BA01"],
        "molecule_structures": {"canonical_smiles": "CC(=O)O", "standard_inchi_key": "K"},
        "molecule_properties": {"full_mwt": "180.16", "alogp": "1.31"},
    }]}))
    [drug] = chemistry.drug_search("aspirin", transport=transport)["results"]

    assert drug["chembl_id"] == "CHEMBL25"
    assert drug["max_phase"] == 4
    # A boxed warning should not need looking for.
    assert drug["black_box_warning"] is True


@pytest.mark.requirement("SCI-P5-22")
def test_bioactivities_keep_the_source_document():
    transport = StubTransport(ok({
        "page_meta": {"total_count": 812},
        "activities": [{
            "target_chembl_id": "CHEMBL204", "target_pref_name": "COX-1",
            "target_organism": "Homo sapiens", "standard_type": "IC50",
            "standard_relation": "=", "standard_value": "1.2",
            "standard_units": "nM", "pchembl_value": "8.9",
            "assay_description": "Inhibition of COX-1",
            "document_chembl_id": "CHEMBL1139451",
        }],
    }))
    result = chemistry.bioactivities("CHEMBL25", transport=transport)

    assert result["total"] == 812
    [activity] = result["results"]
    assert activity["type"] == "IC50" and activity["units"] == "nM"
    # The document id is what makes a number checkable rather than asserted.
    assert activity["document_chembl_id"] == "CHEMBL1139451"


# ── SCI-P5-30 / 31 tool surface ─────────────────────────────────────


@pytest.mark.requirement("SCI-P5-30")
def test_biodata_toolset_matches_the_registry():
    import tools.biodata_tools  # noqa: F401 - registers on import
    from toolsets import TOOLSETS
    from tools.registry import registry

    declared = set(TOOLSETS["biodata"]["tools"])
    registered = {
        name for name, entry in registry._tools.items() if entry.toolset == "biodata"
    }
    assert declared == registered


@pytest.mark.requirement("SCI-P5-31")
def test_tool_errors_are_returned_as_data():
    import tools.biodata_tools as biodata_tools

    payload = json.loads(biodata_tools._call(genes.gene_lookup, symbol=""))
    assert payload["source"] == "ensembl"
    assert "required" in payload["error"]


# ── live services ───────────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.requirement("SCI-P5-01")
def test_live_ensembl_and_mygene():
    gene = genes.gene_lookup("BRCA1")
    assert gene["gene_id"] == "ENSG00000012048"
    assert gene["chromosome"] == "17"

    [hit] = genes.resolve_gene("BRCA1", limit=1)["results"]
    assert hit["uniprot_id"] == "P38398"


@pytest.mark.integration
@pytest.mark.requirement("SCI-P5-04")
def test_live_uniprot():
    protein = genes.protein("P38398")
    assert protein["organism"] == "Homo sapiens"
    assert protein["length"] > 1000


@pytest.mark.integration
@pytest.mark.requirement("SCI-P5-11")
def test_live_gnomad_gene_variants_then_frequency():
    listed = variants.gene_variants("PCSK9", consequence="missense_variant", limit=3)
    assert listed["total"] > 100
    frequency = variants.variant_frequency(listed["results"][0]["variant_id"])
    assert frequency["exome"] or frequency["genome"]


@pytest.mark.integration
@pytest.mark.requirement("SCI-P5-12")
def test_live_clinvar_round_trip():
    found = variants.clinvar_search("BRCA1 pathogenic", limit=3)
    assert found["total"] > 0
    records = variants.clinvar_records(found["ids"][:2])
    assert all(r["review_status"] for r in records["results"])


@pytest.mark.integration
@pytest.mark.requirement("SCI-P5-20")
def test_live_pubchem_and_chembl():
    aspirin = chemistry.compound("aspirin")
    assert aspirin["cid"] == 2244
    assert aspirin["formula"] == "C9H8O4"

    [drug] = chemistry.drug_search("aspirin", limit=1)["results"]
    assert drug["chembl_id"] == "CHEMBL25"
