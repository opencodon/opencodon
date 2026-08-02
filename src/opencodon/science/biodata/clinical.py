"""Clinical and regulatory — ClinicalTrials.gov and openFDA.

What has been tried in people, and what a regulator approved.

Both sources carry a standing caveat that travels with every response rather
than living only in documentation. openFDA states plainly that its data should
not be relied on for decisions about medical care; trial registrations are
sponsor-submitted and a registered trial is not a result. Neither point
survives being left implicit once a payload is a few hops from the reader, so
each response carries it.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from opencodon.science.apiclient import ApiClient, ApiError, bounded_count, clip

TRIALS_URL = "https://clinicaltrials.gov/api/v2"
OPENFDA_URL = "https://api.fda.gov"

SOURCE_TRIALS = "clinicaltrials"
SOURCE_OPENFDA = "openfda"

RESEARCH_ONLY = (
    "Research and informational use only — not a substitute for professional "
    "medical judgement."
)

TRIAL_STATUSES = (
    "NOT_YET_RECRUITING", "RECRUITING", "ACTIVE_NOT_RECRUITING",
    "COMPLETED", "TERMINATED", "WITHDRAWN", "SUSPENDED", "UNKNOWN",
)


def _require(value: str, what: str, source: str) -> str:
    if not (value or "").strip():
        raise ApiError(source, f"a {what} is required")
    return value.strip()


# ── ClinicalTrials.gov ──────────────────────────────────────────────


def _shape_study(study: Dict[str, Any]) -> Dict[str, Any]:
    protocol = study.get("protocolSection") or {}
    ident = protocol.get("identificationModule") or {}
    status = protocol.get("statusModule") or {}
    design = protocol.get("designModule") or {}
    eligibility = protocol.get("eligibilityModule") or {}
    sponsor = (protocol.get("sponsorCollaboratorsModule") or {}).get("leadSponsor") or {}
    conditions = (protocol.get("conditionsModule") or {}).get("conditions") or []
    arms = (protocol.get("armsInterventionsModule") or {}).get("interventions") or []
    outcomes = (protocol.get("outcomesModule") or {}).get("primaryOutcomes") or []

    return {
        "nct_id": ident.get("nctId"),
        "title": ident.get("briefTitle"),
        "status": status.get("overallStatus"),
        "phase": (design.get("phases") or [None])[0],
        "study_type": design.get("studyType"),
        "enrollment": (design.get("enrollmentInfo") or {}).get("count"),
        "start_date": (status.get("startDateStruct") or {}).get("date"),
        "completion_date": (status.get("completionDateStruct") or {}).get("date"),
        "sponsor": sponsor.get("name"),
        "conditions": conditions[:10],
        "interventions": [
            f"{i.get('type')}: {i.get('name')}" for i in arms[:10] if i.get("name")
        ],
        # The endpoint is what a trial actually tested; a title rarely says.
        "primary_outcomes": [
            clip(o.get("measure"), 200) for o in outcomes[:5] if o.get("measure")
        ],
        "eligibility_sex": eligibility.get("sex"),
        "minimum_age": eligibility.get("minimumAge"),
        "maximum_age": eligibility.get("maximumAge"),
    }


def trial_search(
    query: str,
    *,
    status: Optional[str] = None,
    phase: Optional[str] = None,
    limit: Optional[int] = None,
    **kwargs,
) -> Dict[str, Any]:
    """Search ClinicalTrials.gov."""
    query = _require(query, "search term", SOURCE_TRIALS)
    if status and status.upper() not in TRIAL_STATUSES:
        raise ApiError(
            SOURCE_TRIALS,
            f"status must be one of {', '.join(TRIAL_STATUSES)}, got {status!r}",
        )
    count = bounded_count(limit)
    params: Dict[str, Any] = {
        "query.term": query, "pageSize": count, "countTotal": "true",
    }
    if status:
        params["filter.overallStatus"] = status.upper()
    if phase:
        params["query.term"] = f"{query} AREA[Phase]{phase}"

    payload = ApiClient(SOURCE_TRIALS, TRIALS_URL, **kwargs).get_json(
        "/studies", params
    )
    studies = payload.get("studies") or []
    return {
        "source": SOURCE_TRIALS,
        "query": query,
        "total": payload.get("totalCount"),
        "returned": len(studies),
        # A registration is not a result: registered, ongoing and terminated
        # trials all appear here and only `status` distinguishes them.
        "disclaimer": RESEARCH_ONLY,
        "results": [_shape_study(study) for study in studies],
    }


def trial_record(nct_id: str, **kwargs) -> Dict[str, Any]:
    """One trial by NCT identifier."""
    nct_id = _require(nct_id, "NCT identifier", SOURCE_TRIALS).upper()
    payload = ApiClient(SOURCE_TRIALS, TRIALS_URL, **kwargs).get_json(
        f"/studies/{nct_id}"
    )
    if not payload:
        raise ApiError(SOURCE_TRIALS, f"no trial {nct_id!r}", status=404)
    return {"source": SOURCE_TRIALS, "disclaimer": RESEARCH_ONLY,
            **_shape_study(payload)}


# ── openFDA ─────────────────────────────────────────────────────────


def _openfda(**kwargs) -> ApiClient:
    return ApiClient(SOURCE_OPENFDA, OPENFDA_URL, **kwargs)


def drug_label(query: str, *, limit: Optional[int] = None, **kwargs) -> Dict[str, Any]:
    """Structured product labels — indications, warnings, contraindications."""
    query = _require(query, "drug name", SOURCE_OPENFDA)
    count = bounded_count(limit)
    payload = _openfda(**kwargs).get_json(
        "/drug/label.json",
        {"search": f'openfda.brand_name:"{query}" openfda.generic_name:"{query}"',
         "limit": count},
    )
    results = []
    for row in payload.get("results") or []:
        openfda = row.get("openfda") or {}
        results.append({
            "brand_names": (openfda.get("brand_name") or [])[:5],
            "generic_names": (openfda.get("generic_name") or [])[:5],
            "manufacturer": (openfda.get("manufacturer_name") or [None])[0],
            "route": (openfda.get("route") or [])[:5],
            "indications": clip(" ".join(row.get("indications_and_usage") or [])),
            "warnings": clip(" ".join(row.get("warnings") or [])),
            "boxed_warning": clip(" ".join(row.get("boxed_warning") or [])),
            "contraindications": clip(" ".join(row.get("contraindications") or [])),
        })
    return {
        "source": SOURCE_OPENFDA,
        "query": query,
        "total": ((payload.get("meta") or {}).get("results") or {}).get("total"),
        "returned": len(results),
        # openFDA's own position, carried rather than left in its docs.
        "disclaimer": (payload.get("meta") or {}).get("disclaimer") or RESEARCH_ONLY,
        "results": results,
    }


def drug_approvals(query: str, *, limit: Optional[int] = None, **kwargs) -> Dict[str, Any]:
    """Drugs@FDA application records — sponsor, approval, marketing status."""
    query = _require(query, "drug name", SOURCE_OPENFDA)
    count = bounded_count(limit)
    payload = _openfda(**kwargs).get_json(
        "/drug/drugsfda.json",
        {"search": f'openfda.brand_name:"{query}" openfda.generic_name:"{query}"',
         "limit": count},
    )
    results: List[Dict[str, Any]] = []
    for row in payload.get("results") or []:
        products = row.get("products") or []
        submissions = row.get("submissions") or []
        approvals = [
            s.get("submission_status_date") for s in submissions
            if s.get("submission_status") == "AP" and s.get("submission_status_date")
        ]
        results.append({
            "application_number": row.get("application_number"),
            "sponsor": row.get("sponsor_name"),
            "products": [
                {
                    "brand_name": p.get("brand_name"),
                    "dosage_form": p.get("dosage_form"),
                    "strength": (p.get("active_ingredients") or [{}])[0].get("strength"),
                    "marketing_status": p.get("marketing_status"),
                }
                for p in products[:5]
            ],
            "first_approval": min(approvals) if approvals else None,
            "submission_count": len(submissions),
        })
    return {
        "source": SOURCE_OPENFDA,
        "query": query,
        "total": ((payload.get("meta") or {}).get("results") or {}).get("total"),
        "returned": len(results),
        "disclaimer": (payload.get("meta") or {}).get("disclaimer") or RESEARCH_ONLY,
        "results": results,
    }
