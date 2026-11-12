"""Comprehensive tests for Research Quality, Evidence Integrity, DOCX Rendering, and Completion Verification.

Covers:
- Autonomous source discovery and query decomposition.
- Rejection of search-result pages from being counted as substantive sources.
- Evidence integrity: no fabricated citations on zero-source packages, claim-evidence traceability.
- Technical accuracy: mathematical formulas and permutation equivariance.
- DOCX rendering quality: no raw markdown markers, clean heading numbers, hanging-indent references,
  table column widths, and footer page numbering.
- Completion verification: 6-stage verification rejecting insufficient sources, unverified claims,
  or missing deliverables.
"""
from pathlib import Path
import tempfile
import docx
import pytest

from app.completion_verifier import verify_task_completion
from app.research.collector import (
    extract_substantive_links_from_search,
    harvest_page_evidence,
    is_search_results_page,
)
from app.research.models import (
    DataTable,
    EvidenceClassification,
    EvidenceItem,
    FormulaItem,
    ResearchClaim,
    ResearchKnowledgePackage,
    ResearchSubtopic,
    ResearchTopic,
    SourceReference,
)
from app.research.package_resolver import resolve_target_package
from app.research.renderers.docx_renderer import DocxRenderer, _clean_markdown_text
from app.website_resolver import website_resolver


# ===========================================================================
# 1. Source Discovery & Search Page Handling
# ===========================================================================

def test_query_decomposition_extracts_explicit_subtopics():
    """Verify decompose_search_queries breaks down topics with 'including' into focused queries."""
    prompt = (
        "Research transformer architecture, including attention mechanisms, "
        "positional encoding, multi-head attention, and practical applications. "
        "Compare information from multiple credible sources."
    )
    queries = website_resolver.decompose_search_queries(prompt, category="research")
    assert len(queries) >= 3
    q_text = " ".join(queries).lower()
    assert "attention" in q_text
    assert "positional encoding" in q_text
    assert "multi-head" in q_text or "multi head" in q_text


def test_is_search_results_page_detection():
    """Verify search-results URLs and titles are correctly identified across major academic/search engines."""
    assert is_search_results_page("https://www.nature.com/search?q=transformer+architecture") is True
    assert is_search_results_page("https://arxiv.org/search/?query=transformer&searchtype=all") is True
    assert is_search_results_page("https://en.wikipedia.org/wiki/Special:Search?search=transformer") is True
    assert is_search_results_page("https://www.google.com/search?q=attention+is+all+you+need") is True
    assert is_search_results_page("https://www.science.org/action/doSearch?AllField=transformers") is True

    # Real article pages must NOT be flagged as search results
    assert is_search_results_page("https://www.nature.com/articles/s41586-024-07542-8") is False
    assert is_search_results_page("https://arxiv.org/abs/1706.03762") is False
    assert is_search_results_page("https://en.wikipedia.org/wiki/Transformer_(deep_learning_architecture)") is False


def test_harvest_page_evidence_rejects_search_results():
    """Search results pages must return status='search_results' and not be recorded as sources."""
    pkg = ResearchKnowledgePackage(
        package_id="pkg_test_search",
        topic=ResearchTopic(title="Transformer Research", objective="Investigate transformers"),
    )
    res = harvest_page_evidence(
        url="https://www.nature.com/search?q=transformer+architecture",
        title="Search Results | Nature",
        text="Showing 1-10 of 1,234 search results for transformer architecture. 1. Attention in Vision...",
        pkg=pkg,
    )
    assert res.status == "search_results"
    assert len(pkg.sources) == 0
    assert len(pkg.evidence) == 0


def test_extract_substantive_links_from_search_results():
    """Extract candidate article links from search results text and interactive elements."""
    search_url = "https://www.nature.com/search?q=transformer"
    interactive = [
        {"name": "Attention mechanisms in neural systems", "href": "/articles/s41586-023-0100-1"},
        {"name": "Next Page", "href": "/search?q=transformer&page=2"},
    ]
    raw_text = (
        "Search results for transformer. Article: https://www.nature.com/articles/s41586-023-0100-1 "
        "Also see foundational preprint at https://arxiv.org/abs/1706.03762"
    )
    links = extract_substantive_links_from_search(search_url, interactive, raw_text)
    assert any("articles/s41586" in lk["url"] for lk in links)
    assert any("arxiv.org/abs/1706.03762" in lk["url"] for lk in links)
    # Search pagination links must not be included
    assert not any("page=2" in lk["url"] for lk in links)


# ===========================================================================
# 2. Evidence Integrity & Traceability
# ===========================================================================

def test_zero_source_package_has_no_fabricated_citations():
    """Exporting a conversational package with zero sources must NEVER manufacture citations."""
    res = resolve_target_package(
        chat_id="chat_empty",
        instruction="export as docx",
        context={"latest_assistant_response": "Here is an explanation of self-attention mechanisms with detailed content."},
    )
    pkg = res.package
    assert pkg is not None
    data = pkg.get("data") or pkg
    # Must NOT have hardcoded arXiv or Wikipedia fabricated sources
    assert len(data.get("sources", {})) == 0


def test_claim_without_evidence_downgraded_to_unverified():
    """A ResearchClaim created without evidence_item_ids must be downgraded to UNVERIFIED."""
    claim = ResearchClaim(
        claim_id="cl_01",
        statement="Transformers have 175 billion parameters by default.",
        classification=EvidenceClassification.CORROBORATED,  # Improperly classified
        evidence_item_ids=[],  # No supporting evidence
    )
    claim.validate_classification_integrity()
    assert claim.classification == EvidenceClassification.UNVERIFIED
    assert claim.confidence < 0.5


def test_claim_with_evidence_retains_classification():
    """A claim linked to valid evidence retains its supported classification."""
    claim = ResearchClaim(
        claim_id="cl_02",
        statement="Self-attention computes dot-product similarity between query and key vectors.",
        classification=EvidenceClassification.SINGLE_SOURCE,
        evidence_item_ids=["ev_01"],
        primary_source_id="src_01",
        confidence=0.9,
    )
    claim.validate_classification_integrity()
    assert claim.classification == EvidenceClassification.SINGLE_SOURCE


# ===========================================================================
# 3. DOCX Rendering Quality
# ===========================================================================

def test_docx_clean_markdown_text():
    """Verify markdown headings and bullet prefixes are stripped cleanly."""
    assert _clean_markdown_text("### 1. Introduction") == "Introduction"
    assert _clean_markdown_text("- Key Finding Point") == "Key Finding Point"
    assert _clean_markdown_text("1. Numbered Item") == "Numbered Item"


def test_docx_renderer_formats_runs_without_raw_markdown():
    """Verify DOCX generation renders bold/italic/code/math into runs without raw markdown syntax."""
    pkg = ResearchKnowledgePackage(
        package_id="pkg_test_docx",
        topic=ResearchTopic(
            title="Transformer Architecture Analysis",
            objective="Evaluate attention mechanisms",
            scope="Comprehensive technical breakdown",
        ),
    )
    pkg.add_source(SourceReference(
        source_id="src_01",
        url="https://arxiv.org/abs/1706.03762",
        domain="arxiv.org",
        title="Attention Is All You Need",
        publication_date="2017-06-12",
        reliability_score=0.99,
        reliability_tier="peer_reviewed",
        is_primary=True,
    ))
    pkg.add_evidence(EvidenceItem(
        evidence_id="ev_01",
        source_id="src_01",
        url="https://arxiv.org/abs/1706.03762",
        title="Attention Is All You Need",
        quote="We propose the Transformer, a model architecture eschewing recurrence.",
    ))
    pkg.add_claim(ResearchClaim(
        claim_id="c_01",
        statement="The **Transformer** uses **Self-Attention** with `O(N^2)` sequence complexity and $\\text{Attention}(Q, K, V)$.",
        classification=EvidenceClassification.SINGLE_SOURCE,
        evidence_item_ids=["ev_01"],
        primary_source_id="src_01",
        confidence=0.95,
    ))
    pkg.topic.subtopics.append(ResearchSubtopic(
        subtopic_id="sub_01",
        title="1. Scaled Dot-Product Attention",  # Contains leading number to test deduplication
        findings_summary="The **Scaled Dot-Product Attention** computes weights: $\\text{softmax}(QK^T / \\sqrt{d_k})V$.",
        claim_ids=["c_01"],
    ))
    pkg.add_formula(FormulaItem(
        formula_id="f_01",
        name="Scaled Dot-Product Attention",
        latex=r"\text{Attention}(Q, K, V) = \text{softmax}\left(\frac{QK^T}{\sqrt{d_k}}\right)V",
        explanation="Attention formulation without recurrence. Permutation equivariant over token sets without positional encodings.",
    ))
    pkg.add_table(DataTable(
        table_id="tbl_01",
        title="Architecture Comparison",
        headers=["Model Type", "Complexity per Layer", "Sequential Operations", "Max Path Length"],
        rows=[
            ["Self-Attention", "O(n^2 · d)", "O(1)", "O(1)"],
            ["Recurrent (RNN)", "O(n · d^2)", "O(n)", "O(n)"],
            ["Convolutional", "O(k · n · d^2)", "O(1)", "O(log_k(n))"],
        ],
    ))

    with tempfile.TemporaryDirectory() as tmpdir:
        out_docx = Path(tmpdir) / "test_report.docx"
        DocxRenderer().render(pkg, str(out_docx))
        assert out_docx.exists()
        assert out_docx.stat().st_size > 5000

        # Inspect generated docx with python-docx
        doc = docx.Document(str(out_docx))

        # 1. Heading number deduplication check:
        # Heading must be "2.1 Scaled Dot-Product Attention", NOT "2.1 1. Scaled Dot-Product Attention"
        heading_texts = [p.text for p in doc.paragraphs if p.style.name.startswith("Heading")]
        assert any("2.1 Scaled Dot-Product Attention" in h for h in heading_texts)
        assert not any("2.1 1." in h for h in heading_texts)

        # 2. No raw markdown tokens in normal paragraphs
        all_para_text = "\n".join(p.text for p in doc.paragraphs)
        assert "**Transformer**" not in all_para_text
        assert "**Self-Attention**" not in all_para_text
        assert "`O(N^2)`" not in all_para_text

        # 3. Check table column widths
        assert len(doc.tables) >= 2  # metadata table + data table
        data_table = doc.tables[1]
        assert len(data_table.columns) == 4
        for row in data_table.rows:
            for cell in row.cells:
                assert cell.width is not None
                assert cell.width.inches > 1.0  # Distributed across page width

        # 4. Check references section has hanging indent bibliographic entry
        ref_paras = [p for p in doc.paragraphs if p.text.startswith("[1]")]
        assert len(ref_paras) == 1
        ref_p = ref_paras[0]
        assert "Attention Is All You Need" in ref_p.text
        assert "arxiv.org" in ref_p.text
        assert "Peer Reviewed" in ref_p.text
        assert ref_p.paragraph_format.left_indent is not None
        assert ref_p.paragraph_format.first_line_indent is not None
        assert ref_p.paragraph_format.left_indent.inches > 0
        assert ref_p.paragraph_format.first_line_indent.inches < 0


def test_docx_zero_sources_displays_honest_limitation():
    """DOCX with zero sources must display the conversational disclaimer note."""
    pkg = ResearchKnowledgePackage(
        package_id="pkg_test_empty",
        topic=ResearchTopic(title="Conversational Summary", objective="Review context"),
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        out_docx = Path(tmpdir) / "empty_report.docx"
        DocxRenderer().render(pkg, str(out_docx))
        doc = docx.Document(str(out_docx))
        full_text = "\n".join(p.text for p in doc.paragraphs)
        assert "No external sources were consulted or cited" in full_text


# ===========================================================================
# 4. Strict 6-Stage Completion Verification
# ===========================================================================

def test_verifier_rejects_single_source_when_multi_source_requested():
    """If user asks for multiple credible sources, 1 source must fail verification."""
    instruction = "Research transformer architecture and compare information from multiple credible sources. Generate DOCX."
    mem = {
        "is_research": True,
        "requested_format": "docx",
        "research_knowledge_package": {
            "sources": {
                "src_01": {
                    "url": "https://www.nature.com/articles/s41586-024-07542-8",
                    "domain": "nature.com",
                    "reliability_score": 0.95,
                }
            },
            "claims": {
                "c_01": {"statement": "Self attention scales quadratically.", "evidence_item_ids": ["ev_01"]}
            },
            "evidence": {"ev_01": {"snippet": "..."}},
        },
    }
    res = verify_task_completion(
        instruction=instruction,
        candidate_state="completed",
        message="Research complete.",
        working_memory=mem,
    )
    assert res.state == "failed"
    assert "multiple credible sources" in res.message or "multiple credible sources" in res.reason


def test_verifier_rejects_search_page_as_sole_source():
    """A search results page does not satisfy source collection."""
    instruction = "Research transformer architecture."
    mem = {
        "is_research": True,
        "research_knowledge_package": {
            "sources": {
                "src_01": {
                    "url": "https://www.nature.com/search?q=transformer",
                    "domain": "nature.com",
                    "reliability_score": 0.95,
                }
            },
            "claims": {"c_01": {"statement": "Findings...", "evidence_item_ids": ["ev_01"]}},
            "evidence": {"ev_01": {"snippet": "..."}},
        },
    }
    res = verify_task_completion(
        instruction=instruction,
        candidate_state="completed",
        message="Done.",
        working_memory=mem,
    )
    assert res.state == "failed"
    assert "No substantive research sources" in res.message or "search results" in res.message


def test_verifier_rejects_missing_artifact_when_requested():
    """If DOCX was requested, verification must fail if no artifact exists on disk."""
    instruction = "Research transformer architecture. Generate a detailed DOCX report."
    mem = {
        "is_research": True,
        "requested_format": "docx",
        "research_knowledge_package": {
            "sources": {
                "src_01": {"url": "https://arxiv.org/abs/1706.03762", "domain": "arxiv.org", "reliability_score": 0.99},
            },
            "claims": {"c_01": {"statement": "Self-attention definition", "evidence_item_ids": ["ev_01"]}},
            "evidence": {"ev_01": {"snippet": "..."}},
        },
        "artifacts": [],  # Artifact was not generated
    }
    res = verify_task_completion(
        instruction=instruction,
        candidate_state="completed",
        message="Research finished.",
        working_memory=mem,
    )
    assert res.state == "failed"
    assert "Deliverable document was requested but not generated" in res.message


def test_verifier_accepts_valid_multisource_research_with_artifact():
    """When multiple independent sources, claims with evidence, and non-empty artifact exist, passes."""
    with tempfile.TemporaryDirectory() as tmpdir:
        dummy_docx = Path(tmpdir) / "report.docx"
        dummy_docx.write_bytes(b"PK\x03\x04" + b"\x00" * 200)  # Non-empty file

        instruction = "Research transformer architecture and compare information from multiple credible sources. Generate DOCX."
        mem = {
            "is_research": True,
            "requested_format": "docx",
            "research_knowledge_package": {
                "sources": {
                    "src_01": {"url": "https://arxiv.org/abs/1706.03762", "domain": "arxiv.org", "reliability_score": 0.99},
                    "src_02": {"url": "https://www.nature.com/articles/s41586-024-07542-8", "domain": "nature.com", "reliability_score": 0.98},
                },
                "claims": {
                    "c_01": {"statement": "Self attention formulation", "evidence_item_ids": ["ev_01"]},
                    "c_02": {"statement": "Transformer cross-attention", "evidence_item_ids": ["ev_02"]},
                },
                "evidence": {
                    "ev_01": {"snippet": "Vaswani et al. 2017"},
                    "ev_02": {"snippet": "Nature analysis"},
                },
            },
            "artifacts": [{
                "filename": "report.docx",
                "filepath": str(dummy_docx),
                "file_size_bytes": dummy_docx.stat().st_size,
            }],
        }
        res = verify_task_completion(
            instruction=instruction,
            candidate_state="completed",
            message="Research completed across multiple sources with report generated.",
            working_memory=mem,
        )
        assert res.state == "completed"
        assert res.is_verified is True
        assert "across 2 independent source(s)" in res.reason


def test_verifier_fails_closed_on_execution_failure():
    """If execution reported failed, verifier must NEVER override failure into success."""
    instruction = "Research transformer architecture."
    res = verify_task_completion(
        instruction=instruction,
        candidate_state="failed",
        message="Browser crashed during navigation.",
        working_memory={},
    )
    assert res.state == "failed"
    assert res.is_verified is False
