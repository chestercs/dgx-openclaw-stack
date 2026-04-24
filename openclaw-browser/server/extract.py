"""HTML → markdown extraction.

trafilatura is the primary extractor (F1 ≈ 0.96 on the BoilerNet evaluation
set, multilingual including Hungarian, MIT). It internally cascades to
readability-lxml on pages where its primary score is low; we explicitly
import readability-lxml as a backup so the cascade has a deterministic
fallback even on offline / cache-only paths.

Why this lives in our service and not in OpenClaw: OpenClaw's `browser`
tool returns accessibility-tree snapshots (with stable ref IDs for the
agent to drive clicks), NOT human-readable markdown. For research notes
and Studio lore ingestion, the agent wants clean markdown — so it calls
our `/v1/extract` after grabbing HTML via `browser.evaluate`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import trafilatura
from readability import Document  # readability-lxml

log = logging.getLogger("browser.extract")


@dataclass
class ExtractionResult:
    markdown: str
    title: str | None
    url: str | None
    word_count: int
    extractor: str  # "trafilatura" | "readability"


def extract_markdown(
    html: str,
    *,
    url: str | None = None,
    favor_recall: bool = False,
) -> ExtractionResult:
    """Extract main content as markdown. Tries trafilatura first; falls back
    to readability-lxml if trafilatura returns nothing usable.

    `favor_recall=True` widens trafilatura's content threshold — useful on
    light pages (one paragraph blogs, README-style docs) where the default
    "favor precision" mode would emit nothing.
    """
    title: str | None = None
    extractor = "trafilatura"
    md = trafilatura.extract(
        html,
        url=url,
        output_format="markdown",
        include_links=True,
        include_images=False,
        favor_recall=favor_recall,
        include_tables=True,
        with_metadata=False,
    )
    if not md or not md.strip():
        log.info("trafilatura returned empty — falling back to readability-lxml")
        try:
            doc = Document(html)
            title = doc.short_title() or None
            cleaned_html = doc.summary(html_partial=True)
            md = trafilatura.extract(
                cleaned_html,
                output_format="markdown",
                include_links=True,
                include_images=False,
                favor_recall=True,
            ) or ""
            extractor = "readability"
        except Exception as exc:
            log.warning("readability-lxml fallback failed: %s", exc)
            md = ""
            extractor = "failed"

    if title is None:
        # trafilatura's metadata path is the cheapest way to get a title;
        # bare_extraction returns a small dataclass-like object.
        try:
            meta = trafilatura.bare_extraction(html, url=url, with_metadata=True)
            if meta:
                title = getattr(meta, "title", None) or (
                    meta.get("title") if isinstance(meta, dict) else None
                )
        except Exception:
            pass

    md = (md or "").strip()
    word_count = len(md.split()) if md else 0
    return ExtractionResult(
        markdown=md,
        title=title,
        url=url,
        word_count=word_count,
        extractor=extractor,
    )
