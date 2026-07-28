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

from science.literature import crossref, openalex, pubmed
from science.literature.client import (
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
    import tools.literature_tools  # noqa: F401 - registers on import
    from toolsets import TOOLSETS
    from tools.registry import registry

    declared = set(TOOLSETS["literature"]["tools"])
    registered = {
        name for name, entry in registry._tools.items()
        if entry.toolset == "literature"
    }
    assert declared == registered, "toolsets.py and the registry disagree"


@pytest.mark.requirement("SCI-P2-20")
def test_tool_errors_come_back_as_data_not_exceptions():
    import tools.literature_tools as literature_tools

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
