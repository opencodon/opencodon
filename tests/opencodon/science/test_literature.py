"""Literature layer — transport behaviour, source shaping, and tool surface.

Almost everything here runs against a stub transport rather than the live
services. That is deliberate: retry, backoff and rate-limit handling are
exactly the behaviours you cannot provoke on demand against a healthy public
API, and a test that depends on OpenAlex being reachable tells you about
OpenAlex rather than about this code.

The live services are covered separately by the ``integration``-marked tests
at the bottom, which the default pytest run excludes.
"""

import json

import httpx
import pytest

from opencodon.science.literature import arxiv, biorxiv, crossref, europepmc, openalex, pubmed
from opencodon.science.literature.client import (
    MAX_RESULTS,
    ScholarlyClient,
    ScholarlyError,
    bounded_count,
    clip,
    user_agent,
)


# ── stub transport ──────────────────────────────────────────────────


class RecordingTransport(httpx.BaseTransport):
    """Serves canned responses and records what was asked for.

    ``responses`` is consumed in order; the last entry repeats once exhausted,
    so a retry test can say "fail twice then succeed" without padding.
    """

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def handle_request(self, request):
        self.requests.append(request)
        spec = (
            self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        )
        status, body, headers = spec
        content = body if isinstance(body, bytes) else str(body).encode()
        return httpx.Response(status, content=content, headers=headers or {},
                              request=request)


def json_response(payload, status=200, headers=None):
    return (status, json.dumps(payload), {**(headers or {}), "Content-Type": "application/json"})


@pytest.fixture
def no_sleep():
    """Collect backoff durations instead of waiting them out."""
    slept = []
    yield slept, slept.append


# ── SCI-P2-01 politeness ────────────────────────────────────────────


@pytest.mark.requirement("SCI-P2-01")
def test_user_agent_names_the_project(monkeypatch):
    monkeypatch.delenv("OPENCODON_SCHOLARLY_MAILTO", raising=False)
    agent = user_agent()
    assert agent.startswith("opencodon/")
    assert "github.com/opencodon" in agent
    # No contact configured: the header must not invent one.
    assert "mailto:" not in agent


@pytest.mark.requirement("SCI-P2-01")
def test_user_agent_carries_configured_contact(monkeypatch):
    monkeypatch.setenv("OPENCODON_SCHOLARLY_MAILTO", "team@example.org")
    assert "mailto:team@example.org" in user_agent()


@pytest.mark.requirement("SCI-P2-01")
def test_requests_send_the_polite_headers(monkeypatch):
    monkeypatch.setenv("OPENCODON_SCHOLARLY_MAILTO", "team@example.org")
    transport = RecordingTransport(json_response({"ok": True}))
    client = ScholarlyClient("demo", "https://example.org", transport=transport)
    client.get_json("/thing")

    sent = transport.requests[0]
    assert "mailto:team@example.org" in sent.headers["User-Agent"]


# ── SCI-P2-02 retry ─────────────────────────────────────────────────


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
@pytest.mark.requirement("SCI-P2-02")
def test_transient_failures_are_retried_then_succeed(status, no_sleep):
    slept, sleeper = no_sleep
    transport = RecordingTransport(
        (status, "upstream wobble", {}),
        json_response({"recovered": True}),
    )
    client = ScholarlyClient(
        "demo", "https://example.org", transport=transport, sleep=sleeper
    )

    assert client.get_json("/thing") == {"recovered": True}
    assert len(transport.requests) == 2
    assert len(slept) == 1


@pytest.mark.requirement("SCI-P2-02")
def test_retry_after_header_is_honoured(no_sleep):
    slept, sleeper = no_sleep
    transport = RecordingTransport(
        (429, "slow down", {"Retry-After": "4"}),
        json_response({"ok": True}),
    )
    client = ScholarlyClient(
        "demo", "https://example.org", transport=transport, sleep=sleeper
    )
    client.get_json("/thing")

    # The server's instruction wins over our own backoff curve, which would
    # otherwise have waited under a second on the first attempt.
    assert slept == [4.0]


@pytest.mark.requirement("SCI-P2-02")
def test_unparseable_retry_after_falls_back_to_backoff(no_sleep):
    slept, sleeper = no_sleep
    transport = RecordingTransport(
        (503, "later", {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}),
        json_response({"ok": True}),
    )
    ScholarlyClient(
        "demo", "https://example.org", transport=transport, sleep=sleeper
    ).get_json("/thing")

    assert len(slept) == 1 and 0 < slept[0] <= 8.0


# ── SCI-P2-03 fail fast and legibly ─────────────────────────────────


@pytest.mark.requirement("SCI-P2-03")
def test_client_error_is_not_retried(no_sleep):
    slept, sleeper = no_sleep
    transport = RecordingTransport((404, "no such record", {}))
    client = ScholarlyClient(
        "demo", "https://example.org", transport=transport, sleep=sleeper
    )

    with pytest.raises(ScholarlyError) as caught:
        client.get_json("/missing")

    assert caught.value.status == 404
    assert caught.value.source == "demo"
    # A missing record is missing on the second ask too.
    assert len(transport.requests) == 1 and slept == []


@pytest.mark.requirement("SCI-P2-03")
def test_exhausted_retries_report_the_source(no_sleep):
    _, sleeper = no_sleep
    transport = RecordingTransport((503, "down", {}))
    client = ScholarlyClient(
        "demo", "https://example.org", transport=transport,
        sleep=sleeper, max_attempts=3,
    )

    with pytest.raises(ScholarlyError) as caught:
        client.get_json("/thing")

    assert len(transport.requests) == 3
    assert caught.value.retryable
    assert "demo" in str(caught.value)


@pytest.mark.requirement("SCI-P2-03")
def test_non_json_body_is_a_structured_error():
    transport = RecordingTransport((200, "<html>nope</html>", {}))
    client = ScholarlyClient("demo", "https://example.org", transport=transport)

    with pytest.raises(ScholarlyError) as caught:
        client.get_json("/thing")
    assert "non-JSON" in str(caught.value)


@pytest.mark.requirement("SCI-P2-03")
def test_malformed_doi_is_rejected_without_a_request():
    for bad in ["", "not-a-doi", "https://example.org/paper", "10.x/y"]:
        with pytest.raises(ScholarlyError):
            crossref.normalise_doi(bad)


@pytest.mark.requirement("SCI-P2-12")
def test_doi_wrappers_are_stripped():
    for wrapped in [
        "10.1038/nature12373",
        "https://doi.org/10.1038/nature12373",
        "doi:10.1038/nature12373",
        "  10.1038/nature12373  ",
    ]:
        assert crossref.normalise_doi(wrapped) == "10.1038/nature12373"


# ── SCI-P2-04 bounded payloads ──────────────────────────────────────


@pytest.mark.requirement("SCI-P2-04")
def test_result_counts_are_capped():
    assert bounded_count(None) == 10
    assert bounded_count(0) == 10
    assert bounded_count(5) == 5
    assert bounded_count(10_000) == MAX_RESULTS


@pytest.mark.requirement("SCI-P2-04")
def test_long_text_is_clipped_and_says_so():
    clipped = clip("word " * 2000, limit=100)
    assert len(clipped) < 200
    assert "more chars" in clipped
    assert clip(None) is None


@pytest.mark.requirement("SCI-P2-04")
def test_author_lists_are_truncated_but_counted():
    work = {
        "title": "Consortium paper",
        "authorships": [
            {"author": {"display_name": f"Author {i}"}} for i in range(500)
        ],
    }
    shaped = openalex._shape_work(work)
    assert len(shaped["authors"]) == 20
    assert shaped["author_count"] == 500


@pytest.mark.requirement("SCI-P2-04")
def test_search_requests_no_more_than_the_cap():
    transport = RecordingTransport(json_response({"results": [], "meta": {"count": 0}}))
    openalex.search_works("crispr", limit=9999, transport=transport)

    assert transport.requests[0].url.params["per-page"] == str(MAX_RESULTS)


# ── SCI-P2-10 / 11 OpenAlex ─────────────────────────────────────────


WORK_PAYLOAD = {
    "id": "https://openalex.org/W2741809807",
    "doi": "https://doi.org/10.1038/nature12373",
    "title": "A study of things",
    "publication_year": 2013,
    "type": "article",
    "cited_by_count": 4242,
    "authorships": [{"author": {"display_name": "Ada Lovelace"}}],
    "primary_location": {"source": {"display_name": "Nature"}},
    "abstract_inverted_index": {"Hello": [0], "world": [1]},
    "referenced_works": ["https://openalex.org/W1", "https://openalex.org/W2"],
}


@pytest.mark.requirement("SCI-P2-10")
def test_abstract_is_reconstructed_from_the_inverted_index():
    index = {"the": [0, 4], "cat": [1], "sat": [2], "on": [3], "mat": [5]}
    assert openalex.reconstruct_abstract(index) == "the cat sat on the mat"
    assert openalex.reconstruct_abstract({}) is None
    assert openalex.reconstruct_abstract(None) is None


@pytest.mark.requirement("SCI-P2-10")
def test_search_shapes_the_fields_a_reader_needs():
    transport = RecordingTransport(
        json_response({"results": [WORK_PAYLOAD], "meta": {"count": 137}})
    )
    result = openalex.search_works("things", transport=transport)

    assert result["total"] == 137
    assert result["returned"] == 1
    [work] = result["results"]
    assert work["title"] == "A study of things"
    assert work["year"] == 2013
    assert work["venue"] == "Nature"
    assert work["cited_by_count"] == 4242
    assert work["abstract"] == "Hello world"


@pytest.mark.requirement("SCI-P2-10")
def test_work_lookup_accepts_doi_and_openalex_forms():
    for identifier, expected in [
        ("10.1038/nature12373", "doi:10.1038/nature12373"),
        ("https://doi.org/10.1038/nature12373", "doi:10.1038/nature12373"),
        ("W2741809807", "W2741809807"),
        ("https://openalex.org/W2741809807", "W2741809807"),
    ]:
        assert openalex._normalise_id(identifier) == expected

    with pytest.raises(ScholarlyError):
        openalex._normalise_id("")


@pytest.mark.requirement("SCI-P2-11")
def test_cited_by_filters_on_the_resolved_work():
    transport = RecordingTransport(
        json_response(WORK_PAYLOAD),
        json_response({"results": [WORK_PAYLOAD], "meta": {"count": 9}}),
    )
    result = openalex.cited_by("10.1038/nature12373", transport=transport)

    assert result["direction"] == "cited_by"
    assert result["total"] == 9
    assert transport.requests[1].url.params["filter"] == "cites:W2741809807"


@pytest.mark.requirement("SCI-P2-11")
def test_references_walk_the_backward_edge():
    transport = RecordingTransport(
        json_response({"id": "https://openalex.org/W2741809807",
                       "referenced_works": ["https://openalex.org/W1",
                                            "https://openalex.org/W2"]}),
        json_response({"results": [WORK_PAYLOAD], "meta": {"count": 2}}),
    )
    result = openalex.references("W2741809807", transport=transport)

    assert result["direction"] == "references"
    assert result["total"] == 2
    assert transport.requests[1].url.params["filter"] == "openalex_id:W1|W2"


@pytest.mark.requirement("SCI-P2-11")
def test_references_of_a_work_that_cites_nothing():
    transport = RecordingTransport(
        json_response({"id": "https://openalex.org/W9", "referenced_works": []})
    )
    result = openalex.references("W9", transport=transport)

    # No second request: there is nothing to look up.
    assert result["results"] == [] and len(transport.requests) == 1


# ── SCI-P2-12 Crossref ──────────────────────────────────────────────


@pytest.mark.requirement("SCI-P2-12")
def test_crossref_shapes_the_deposited_record():
    transport = RecordingTransport(
        json_response({
            "message": {
                "DOI": "10.1038/nature12373",
                "title": ["A study of things"],
                "container-title": ["Nature"],
                "publisher": "Springer",
                "type": "journal-article",
                "issued": {"date-parts": [[2013, 7, 24]]},
                "author": [{"given": "Ada", "family": "Lovelace"}],
                "is-referenced-by-count": 17,
                "abstract": "<jats:p>Some <jats:i>text</jats:i>.</jats:p>",
            }
        })
    )
    result = crossref.get_work("10.1038/nature12373", transport=transport)

    assert result["title"] == "A study of things"
    assert result["container"] == "Nature"
    assert result["issued"] == "2013-07-24"
    assert result["authors"] == ["Ada Lovelace"]
    # JATS markup is stripped rather than handed to the model raw.
    assert "<jats:" not in result["abstract"]
    assert "Some text ." in result["abstract"]


@pytest.mark.requirement("SCI-P2-12")
def test_partial_dates_render_at_their_real_precision():
    assert crossref._date_parts({"date-parts": [[2013]]}) == "2013"
    assert crossref._date_parts({"date-parts": [[2013, 7]]}) == "2013-07"
    assert crossref._date_parts({"date-parts": [[]]}) is None
    assert crossref._date_parts(None) is None


# ── SCI-P2-13 PubMed ────────────────────────────────────────────────


EFETCH_XML = """<?xml version="1.0"?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <Article>
        <ArticleTitle>CRISPR in practice</ArticleTitle>
        <Journal><Title>Nature Methods</Title>
          <JournalIssue><PubDate><Year>2021</Year><Month>03</Month></PubDate></JournalIssue>
        </Journal>
        <Abstract>
          <AbstractText Label="BACKGROUND">Genome editing is useful.</AbstractText>
          <AbstractText Label="RESULTS">It worked.</AbstractText>
        </Abstract>
        <AuthorList>
          <Author><LastName>Doudna</LastName><Initials>JA</Initials></Author>
          <Author><CollectiveName>The Consortium</CollectiveName></Author>
        </AuthorList>
      </Article>
    </MedlineCitation>
    <PubmedData><ArticleIdList>
      <ArticleId IdType="pubmed">33686301</ArticleId>
      <ArticleId IdType="doi">10.1038/s41592-021-01102-w</ArticleId>
    </ArticleIdList></PubmedData>
  </PubmedArticle>
</PubmedArticleSet>
"""


@pytest.mark.requirement("SCI-P2-13")
def test_pubmed_search_returns_pmids_and_the_real_total():
    transport = RecordingTransport(
        json_response({"esearchresult": {"count": "8213", "idlist": ["1", "2", "3"]}})
    )
    result = pubmed.search("crispr", limit=3, transport=transport)

    assert result["pmids"] == ["1", "2", "3"]
    assert result["returned"] == 3
    # The total describes the query, not the page — that is what tells a
    # caller whether the search needs narrowing.
    assert result["total"] == 8213


@pytest.mark.requirement("SCI-P2-13")
def test_pubmed_fetch_parses_structured_abstracts():
    transport = RecordingTransport((200, EFETCH_XML, {}))
    result = pubmed.fetch(["33686301"], transport=transport)

    [article] = result["results"]
    assert article["pmid"] == "33686301"
    assert article["doi"] == "10.1038/s41592-021-01102-w"
    assert article["title"] == "CRISPR in practice"
    assert article["journal"] == "Nature Methods"
    assert article["date"] == "2021-03"
    # Section labels are preserved — dropping them loses the structure that
    # makes a structured abstract worth having.
    assert "BACKGROUND: Genome editing is useful." in article["abstract"]
    assert "RESULTS: It worked." in article["abstract"]
    # Both an individual and a collective author are recognised.
    assert article["authors"] == ["Doudna JA", "The Consortium"]


@pytest.mark.requirement("SCI-P2-13")
def test_pubmed_fetch_accepts_a_comma_string_and_rejects_nothing_at_all():
    transport = RecordingTransport((200, EFETCH_XML, {}))
    pubmed.fetch("33686301, 123", transport=transport)
    assert transport.requests[0].url.params["id"] == "33686301,123"

    with pytest.raises(ScholarlyError):
        pubmed.fetch([])


@pytest.mark.requirement("SCI-P2-13")
def test_unparseable_efetch_xml_is_a_structured_error():
    transport = RecordingTransport((200, "<not-xml", {}))
    with pytest.raises(ScholarlyError) as caught:
        pubmed.fetch(["1"], transport=transport)
    assert "unparseable" in str(caught.value)


# ── SCI-P2-14 identifier conversion ─────────────────────────────────


@pytest.mark.requirement("SCI-P2-14")
def test_id_conversion_keeps_unmapped_inputs_visible():
    transport = RecordingTransport(
        json_response({"records": [
            {"requested-id": "10.1038/nature12373", "pmid": "23851394",
             "pmcid": "PMC3703847", "doi": "10.1038/nature12373"},
            {"requested-id": "10.9999/nope", "status": "error",
             "errmsg": "invalid article id"},
        ]})
    )
    result = pubmed.convert_ids(["10.1038/nature12373", "10.9999/nope"], transport=transport)

    mapped, unmapped = result["records"]
    assert mapped["pmid"] == "23851394" and mapped["pmcid"] == "PMC3703847"
    # An unresolvable id is reported, not silently dropped — otherwise a
    # caller cannot tell a failed lookup from one never attempted.
    assert unmapped["requested"] == "10.9999/nope"
    assert unmapped["error"] == "invalid article id"


@pytest.mark.requirement("SCI-P2-14")
def test_id_conversion_requires_input():
    with pytest.raises(ScholarlyError):
        pubmed.convert_ids([])


# ── SCI-P2-20 tool surface ──────────────────────────────────────────


@pytest.mark.requirement("SCI-P2-20")
def test_literature_toolset_matches_the_registered_tools():
    import opencodon.tools.literature_tools  # noqa: F401 - registers on import
    from toolsets import TOOLSETS
    from opencodon.tools.registry import registry

    declared = set(TOOLSETS["literature"]["tools"])
    registered = {
        name for name, entry in registry._tools.items()
        if entry.toolset == "literature"
    }
    assert declared == registered, "toolsets.py and the registry disagree"


@pytest.mark.requirement("SCI-P2-20")
def test_tool_errors_come_back_as_data_not_exceptions():
    import opencodon.tools.literature_tools as literature_tools

    payload = json.loads(literature_tools._call(crossref.get_work, doi="not-a-doi"))
    assert payload["source"] == "crossref"
    assert "not a well-formed DOI" in payload["error"]


# ── live services ───────────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.requirement("SCI-P2-10")
def test_live_openalex_search():
    result = openalex.search_works("CRISPR gene editing", limit=3)
    assert result["returned"] >= 1
    assert all(work["title"] for work in result["results"])


@pytest.mark.integration
@pytest.mark.requirement("SCI-P2-12")
def test_live_crossref_doi():
    result = crossref.get_work("10.1038/nature12373")
    assert result["doi"].lower() == "10.1038/nature12373"
    assert result["container"]


@pytest.mark.integration
@pytest.mark.requirement("SCI-P2-13")
def test_live_pubmed_round_trip():
    found = pubmed.search("CRISPR", limit=2)
    assert found["total"] > 0
    fetched = pubmed.fetch(found["pmids"])
    assert fetched["returned"] == len(found["pmids"])


# ── SCI-P2-15 bioRxiv / medRxiv ─────────────────────────────────────


PREPRINT = {
    "title": "KCNQ2/3 regulates vestibular afferents",
    "authors": "Sinha, A. K.; Lee, C.; Holt, J. C.",
    "doi": "10.1101/2023.12.30.573731",
    "date": "2024-01-01",
    "version": "1",
    "type": "new results",
    "license": "cc_no",
    "category": "neuroscience",
    "abstract": "Efferent modulation of vestibular afferents.",
    "published": "NA",
    "server": "biorxiv",
}


@pytest.mark.requirement("SCI-P2-15")
def test_preprint_window_shapes_records():
    transport = RecordingTransport(
        json_response({
            "messages": [{"status": "ok", "total": "220", "count": 30}],
            "collection": [PREPRINT],
        })
    )
    result = biorxiv.search_preprints("2024-01-01", "2024-01-02", transport=transport)

    assert result["window_total"] == 220
    [item] = result["results"]
    assert item["doi"] == "10.1101/2023.12.30.573731"
    assert item["category"] == "neuroscience"
    # Semicolon-delimited upstream; a list is what a caller can use.
    assert item["authors"] == ["Sinha, A. K.", "Lee, C.", "Holt, J. C."]
    # "NA" means not yet published — normalised away rather than passed on.
    assert item["published_doi"] is None


@pytest.mark.requirement("SCI-P2-15")
def test_category_filter_reports_that_it_narrowed():
    transport = RecordingTransport(
        json_response({
            "messages": [{"status": "ok", "total": "220"}],
            "collection": [PREPRINT, {**PREPRINT, "category": "genomics"}],
        })
    )
    result = biorxiv.search_preprints(
        "2024-01-01", "2024-01-02", category="neuroscience", transport=transport
    )

    assert result["returned"] == 1
    # window_total describes the window, filtered_from what we actually saw —
    # conflating them would make the filter look lossless when it is not.
    assert result["window_total"] == 220
    assert result["filtered_from"] == 2


@pytest.mark.requirement("SCI-P2-15")
def test_preprint_lookup_returns_the_latest_revision():
    transport = RecordingTransport(
        json_response({"collection": [
            {**PREPRINT, "version": "1"},
            {**PREPRINT, "version": "2", "published": "10.1038/s41586-024-00001"},
        ]})
    )
    result = biorxiv.get_preprint("10.1101/2023.12.30.573731", transport=transport)

    assert result["version"] == "2"
    assert result["published_doi"] == "10.1038/s41586-024-00001"


@pytest.mark.requirement("SCI-P2-15")
def test_missing_preprint_and_bad_server_are_errors():
    transport = RecordingTransport(json_response({"collection": []}))
    with pytest.raises(ScholarlyError) as caught:
        biorxiv.get_preprint("10.1101/nope", transport=transport)
    assert caught.value.status == 404

    with pytest.raises(ScholarlyError):
        biorxiv.search_preprints("2024-01-01", "2024-01-02", server="arxiv")
    with pytest.raises(ScholarlyError):
        biorxiv.search_preprints("", "2024-01-02")


@pytest.mark.requirement("SCI-P2-15")
def test_published_versions_link_preprint_to_journal():
    transport = RecordingTransport(
        json_response({
            "messages": [{"status": "ok", "total": "276"}],
            "collection": [{
                "preprint_doi": "10.1101/2022.09.11.507474",
                "published_doi": "10.1038/s41564-023-01548-y",
                "published_journal": "Nature Microbiology",
                "preprint_title": "Integron cassette dissemination",
                "preprint_authors": "Loot, C.; Millot, G.",
                "preprint_date": "2022-09-12",
                "published_date": "2023-12-01",
            }],
        })
    )
    result = biorxiv.published_versions("2024-01-01", "2024-01-05", transport=transport)

    [item] = result["results"]
    assert item["published_journal"] == "Nature Microbiology"
    assert item["published_doi"] == "10.1038/s41564-023-01548-y"


# ── SCI-P2-17 arXiv ─────────────────────────────────────────────────


ARXIV_ATOM = """<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:arxiv="http://arxiv.org/schemas/atom"
      xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">
  <opensearch:totalResults>109</opensearch:totalResults>
  <entry>
    <id>http://arxiv.org/abs/2202.07171v1</id>
    <updated>2022-02-15T03:47:26Z</updated>
    <published>2022-02-14T01:00:00Z</published>
    <title>Genomic background of CRISPR-Cas genomes</title>
    <summary>CRISPR-Cas systems are an adaptive immunity.</summary>
    <author><name>Ada Lovelace</name></author>
    <author><name>Alan Turing</name></author>
    <arxiv:primary_category term="q-bio.GN"/>
    <category term="q-bio.GN"/>
    <category term="cs.LG"/>
    <arxiv:comment>12 pages, 3 figures</arxiv:comment>
    <link href="https://arxiv.org/abs/2202.07171v1" rel="alternate"/>
    <link href="https://arxiv.org/pdf/2202.07171v1" rel="related" title="pdf"/>
  </entry>
</feed>
"""


@pytest.mark.requirement("SCI-P2-17")
def test_arxiv_atom_is_parsed_to_fields():
    transport = RecordingTransport((200, ARXIV_ATOM, {}))
    result = arxiv.search("crispr", transport=transport)

    assert result["total"] == 109
    [paper] = result["results"]
    assert paper["arxiv_id"] == "2202.07171v1"
    assert paper["title"] == "Genomic background of CRISPR-Cas genomes"
    assert paper["authors"] == ["Ada Lovelace", "Alan Turing"]
    assert paper["primary_category"] == "q-bio.GN"
    assert paper["categories"] == ["q-bio.GN", "cs.LG"]
    assert paper["updated"] == "2022-02-15T03:47:26Z"
    assert paper["pdf_url"].endswith("/pdf/2202.07171v1")
    assert "adaptive immunity" in paper["summary"]


@pytest.mark.requirement("SCI-P2-17")
def test_bare_phrase_is_qualified_but_field_syntax_is_not():
    transport = RecordingTransport((200, ARXIV_ATOM, {}))

    arxiv.search("crispr", transport=transport)
    assert transport.requests[0].url.params["search_query"] == "all:crispr"

    arxiv.search("au:hinton AND cat:cs.LG", transport=transport)
    assert transport.requests[1].url.params["search_query"] == "au:hinton AND cat:cs.LG"


@pytest.mark.requirement("SCI-P2-17")
def test_arxiv_rejects_bad_input():
    with pytest.raises(ScholarlyError):
        arxiv.search("")
    with pytest.raises(ScholarlyError):
        arxiv.search("crispr", sort_by="citations")
    with pytest.raises(ScholarlyError):
        arxiv.get_papers([])

    transport = RecordingTransport((200, "<not-atom", {}))
    with pytest.raises(ScholarlyError) as caught:
        arxiv.search("crispr", transport=transport)
    assert "unparseable" in str(caught.value)


# ── SCI-P2-16 Europe PMC full text ──────────────────────────────────


PMC_SEARCH = {
    "hitCount": 1,
    "resultList": {"result": [{
        "id": "23903748", "source": "MED", "pmid": "23903748",
        "pmcid": "PMC4221854", "doi": "10.1038/nature12373",
        "title": "Nanometre-scale thermometry in a living cell.",
        "journalInfo": {"journal": {"title": "Nature"}},
        "pubYear": "2013", "citedByCount": 500, "isOpenAccess": "Y",
        "fullTextIdList": {"fullTextId": ["PMC4221854"]},
        "authorList": {"author": [{"fullName": "Kucsko G"}]},
        "abstractText": "We demonstrate thermometry.",
    }]},
}

JATS = """<article>
  <front><article-meta>
    <title-group><article-title>Nanometre-scale thermometry</article-title></title-group>
    <abstract><p>We demonstrate thermometry.</p></abstract>
  </article-meta></front>
  <body>
    <sec><title>Introduction</title><p>Temperature matters.</p></sec>
    <sec><title>Methods</title><p>We used nanodiamonds.</p><p>And lasers.</p></sec>
    <sec><title>Results</title><p>It worked well.</p></sec>
  </body>
</article>
"""


@pytest.mark.requirement("SCI-P2-16")
def test_fulltext_is_returned_as_sections():
    transport = RecordingTransport((200, JATS, {}))
    result = europepmc.full_text("PMC4221854", transport=transport)

    assert result["pmcid"] == "PMC4221854"
    assert result["title"] == "Nanometre-scale thermometry"
    titles = [section["title"] for section in result["sections"]]
    assert titles == ["Introduction", "Methods", "Results"]
    # Paragraphs within a section are joined, not flattened into one line.
    methods = result["sections"][1]
    assert "nanodiamonds" in methods["text"] and "lasers" in methods["text"]


@pytest.mark.requirement("SCI-P2-16")
def test_a_single_section_can_be_requested():
    transport = RecordingTransport((200, JATS, {}))
    result = europepmc.full_text("PMC4221854", section="methods", transport=transport)

    assert result["returned_sections"] == 1
    assert result["sections"][0]["title"] == "Methods"


@pytest.mark.requirement("SCI-P2-16")
def test_unknown_section_lists_what_is_available():
    transport = RecordingTransport((200, JATS, {}))
    with pytest.raises(ScholarlyError) as caught:
        europepmc.full_text("PMC4221854", section="discussion", transport=transport)

    message = str(caught.value)
    assert "Introduction" in message and "Methods" in message


@pytest.mark.requirement("SCI-P2-16", "SCI-P2-04")
def test_sections_beyond_the_cap_are_named_not_dropped():
    many = "<article><body>" + "".join(
        f"<sec><title>S{i}</title><p>body {i}</p></sec>" for i in range(20)
    ) + "</body></article>"
    transport = RecordingTransport((200, many, {}))
    result = europepmc.full_text("PMC1", max_sections=3, transport=transport)

    assert result["returned_sections"] == 3
    # The rest are still discoverable — that is what makes the cap safe.
    assert result["omitted_sections"][:2] == ["S3", "S4"]
    assert len(result["omitted_sections"]) == 17


@pytest.mark.requirement("SCI-P2-16")
def test_long_sections_are_clipped():
    long_body = "<article><body><sec><title>S</title><p>" + ("word " * 5000) + "</p></sec></body></article>"
    transport = RecordingTransport((200, long_body, {}))
    result = europepmc.full_text("PMC1", transport=transport)

    section = result["sections"][0]
    assert "more chars" in section["text"]
    # The true length is reported even though the text is cut.
    assert section["chars"] > len(section["text"])


@pytest.mark.requirement("SCI-P2-16")
def test_doi_is_resolved_to_a_pmcid_before_fetching():
    transport = RecordingTransport(json_response(PMC_SEARCH), (200, JATS, {}))
    result = europepmc.full_text("10.1038/nature12373", transport=transport)

    assert result["pmcid"] == "PMC4221854"
    assert transport.requests[0].url.params["query"] == "DOI:10.1038/nature12373"
    assert "PMC4221854/fullTextXML" in str(transport.requests[1].url)


@pytest.mark.requirement("SCI-P2-16")
def test_record_without_open_access_fulltext_says_so():
    transport = RecordingTransport(
        json_response({"hitCount": 1, "resultList": {"result": [
            {"id": "1", "doi": "10.1000/paywalled", "title": "Behind a wall"}
        ]}})
    )
    with pytest.raises(ScholarlyError) as caught:
        europepmc.full_text("10.1000/paywalled", transport=transport)

    assert "no open-access full text" in str(caught.value)
    assert caught.value.status == 404


@pytest.mark.requirement("SCI-P2-16")
def test_pmcid_without_deposited_fulltext_is_explained():
    """Indexed in PMC is not the same as full text being open access."""
    transport = RecordingTransport((404, "Not Found", {}))
    with pytest.raises(ScholarlyError) as caught:
        europepmc.full_text("PMC4221854", transport=transport)

    message = str(caught.value)
    assert "not open access" in message
    assert "only the abstract" in message
    assert caught.value.status == 404


@pytest.mark.requirement("SCI-P2-16")
def test_fulltext_search_flags_retrievability():
    transport = RecordingTransport(json_response(PMC_SEARCH))
    result = europepmc.search("thermometry", transport=transport)

    [item] = result["results"]
    assert item["has_full_text"] is True
    assert item["is_open_access"] is True
    assert item["journal"] == "Nature"


# ── live services (second wave) ─────────────────────────────────────


@pytest.mark.integration
@pytest.mark.requirement("SCI-P2-15")
def test_live_biorxiv_window():
    result = biorxiv.search_preprints("2024-01-01", "2024-01-02", limit=3)
    assert result["returned"] >= 1
    assert all(item["doi"].startswith("10.1101/") for item in result["results"])


@pytest.mark.integration
@pytest.mark.requirement("SCI-P2-17")
def test_live_arxiv_search():
    result = arxiv.search("cat:q-bio.GN", limit=3)
    assert result["returned"] >= 1
    assert all(paper["arxiv_id"] for paper in result["results"])


@pytest.mark.integration
@pytest.mark.requirement("SCI-P2-16")
def test_live_europepmc_fulltext():
    # PMC3703847 has full text deposited; PMC4221854 is indexed but abstract
    # only, which is the case test_pmcid_without_deposited_fulltext covers.
    result = europepmc.full_text("PMC3703847", max_sections=2)
    assert result["returned_sections"] >= 1
    assert result["sections"][0]["text"]
