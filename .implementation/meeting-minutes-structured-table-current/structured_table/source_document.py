"""Source-document helpers that do not pre-split content by Markdown layout."""

from __future__ import annotations

import hashlib
import re
import unicodedata

from .common import clean_evidence_text


def semantic_text(value: str) -> str:
    """Normalize text for evidence location without creating format-based chunks."""

    text = unicodedata.normalize("NFKC", value)
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end >= 0:
            text = text[end + 5 :]
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", text)
    text = re.sub(r"(?m)^\s*>\s?", "", text)
    text = re.sub(r"(?m)^\s*(?:[-*+]\s+|\d+[.)]\s+)", "", text)
    text = re.sub(r"!?\[([^]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"[*_`~]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def locate_semantic_evidence(meeting_markdown: str, source_text: str) -> str:
    """Locate a model-selected semantic quotation in normalized full text."""

    document = semantic_text(meeting_markdown)
    quotation = semantic_text(clean_evidence_text(source_text))
    if quotation:
        starts: list[int] = []
        search_from = 0
        while True:
            start = document.find(quotation, search_from)
            if start < 0:
                break
            starts.append(start)
            search_from = start + 1
        if len(starts) == 1:
            start = starts[0]
            return f"C{start + 1}-C{start + len(quotation)}"
    digest = hashlib.sha256(quotation.encode("utf-8")).hexdigest()[:12]
    return f"Q-{digest}"
