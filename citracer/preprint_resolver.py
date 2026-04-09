"""Map DOIs and URLs to preprint server PDF download URLs.

Supports: bioRxiv, medRxiv, ChemRxiv, SSRN, PsyArXiv, AgriXiv, engrXiv.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)


def build_preprint_pdf_url(doi: str, oa_url: str | None = None) -> str | None:
    """Given a DOI (and optionally an OA URL hint), return a direct PDF URL
    for the paper if it lives on a known preprint server. Returns None if
    the DOI doesn't match any known preprint server.
    """
    doi_lower = doi.lower()

    # --- bioRxiv / medRxiv (share DOI prefix 10.1101) ---
    if doi_lower.startswith("10.1101/"):
        # Disambiguate via OA URL hint if available
        if oa_url and "medrxiv.org" in oa_url:
            return f"https://www.medrxiv.org/content/{doi}v1.full.pdf"
        # Default to bioRxiv (larger repository)
        return f"https://www.biorxiv.org/content/{doi}v1.full.pdf"

    # --- SSRN (DOI prefix 10.2139) ---
    if doi_lower.startswith("10.2139/"):
        # SSRN DOIs look like 10.2139/ssrn.1234567
        m = re.match(r"10\.2139/ssrn\.(\d+)", doi, re.IGNORECASE)
        if m:
            ssrn_id = m.group(1)
            return f"https://papers.ssrn.com/sol3/Delivery.cfm/SSRN_ID{ssrn_id}_code.pdf?abstractid={ssrn_id}"
        return None

    # --- PsyArXiv (DOI prefix 10.31234) ---
    if doi_lower.startswith("10.31234/"):
        # DOI: 10.31234/osf.io/<id>
        m = re.match(r"10\.31234/osf\.io/(\w+)", doi, re.IGNORECASE)
        if m:
            osf_id = m.group(1)
            return f"https://osf.io/{osf_id}/download"
        return None

    # --- engrXiv (DOI prefix 10.31224) ---
    if doi_lower.startswith("10.31224/"):
        m = re.match(r"10\.31224/osf\.io/(\w+)", doi, re.IGNORECASE)
        if m:
            osf_id = m.group(1)
            return f"https://osf.io/{osf_id}/download"
        return None

    # --- AgriXiv (DOI prefix 10.31220) ---
    if doi_lower.startswith("10.31220/"):
        m = re.match(r"10\.31220/osf\.io/(\w+)", doi, re.IGNORECASE)
        if m:
            osf_id = m.group(1)
            return f"https://osf.io/{osf_id}/download"
        return None

    # --- ChemRxiv (DOI prefix 10.26434) ---
    if doi_lower.startswith("10.26434/"):
        # ChemRxiv moved to ACS; newer DOIs use 10.26434/chemrxiv-<id>
        # The PDF is usually at the DOI URL with /v1/download
        return f"https://chemrxiv.org/engage/api-gateway/chemrxiv/assets/orp/resource/item/{doi}/original"

    return None
