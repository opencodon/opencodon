"""Bio-data layer — the public biology databases, shaped for an agent.

Each module wraps a domain rather than a single API, because the useful questions cross services:

- ``genes``      — Ensembl coordinates, MyGene identifier resolution, UniProt
- ``variants``   — gnomAD population frequency, ClinVar clinical assertions
- ``chemistry``  — PubChem structures, ChEMBL bioactivity

All of them ride the shared transport in ``science.apiclient``: polite
identification, retry with backoff, and bounded payloads. Every failure is an
``ApiError``, so the tool layer has one path to translate.
"""

from opencodon.science.apiclient import ApiError

__all__ = ["ApiError"]
