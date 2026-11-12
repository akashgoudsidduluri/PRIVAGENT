"""Regression tests for Step 4.1: Research Intent Routing & Canonical Knowledge Schema.

Verifies:
1. Validated Pydantic models: ResearchKnowledgePackage, ResearchTopic, ResearchSubtopic,
   SourceReference, ResearchClaim, EvidenceItem, DataTable, VisualAsset, FormulaItem.
2. Claim-to-source relationships and evidence metadata preservation.
3. Evidence classifications: user_stated, observed, single_source,
   independently_corroborated, conflicting, unverified.
4. Lossless JSON and dictionary serialization and deserialization.
5. Research-intent and deliverable-format detection in intent_router.py.
6. Guarding against false conversational routing for requests such as:
   - "Research quantum computing and prepare a report."
   - "Explain transformer architecture with diagrams in PDF."
   - "Compare databases in Excel."
   - "Prepare a presentation about renewable energy."
7. Preserving existing non-browser, continuation, and user-tab classification behavior.
"""
from __future__ import annotations

import json
import pytest

from app.intent_router import (
    IntentClassification,
    TaskIntent,
    classify_intent,
    detect_deliverable_type,
    detect_requested_format,
    extract_research_topic,
    is_research_or_deliverable_request,
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
    VisualAsset,
)
from app.session_manager import BrowserSession


# ---------------------------------------------------------------------------
# 1. Pydantic Models & Evidence Classifications
# ---------------------------------------------------------------------------

def test_evidence_classification_variants():
    """Verify all 6 evidence classifications and robust string conversion."""
    assert EvidenceClassification.USER_STATED.value == "user_stated"
    assert EvidenceClassification.OBSERVED.value == "observed"
    assert EvidenceClassification.SINGLE_SOURCE.value == "single_source"
    assert EvidenceClassification.CORROBORATED.value == "independently_corroborated"
    assert EvidenceClassification.CONFLICTING.value == "conflicting"
    assert EvidenceClassification.UNVERIFIED.value == "unverified"

    assert EvidenceClassification.from_str("user_stated") == EvidenceClassification.USER_STATED
    assert EvidenceClassification.from_str("observed") == EvidenceClassification.OBSERVED
    assert EvidenceClassification.from_str("single-source supported") == EvidenceClassification.SINGLE_SOURCE
    assert EvidenceClassification.from_str("independently corroborated") == EvidenceClassification.CORROBORATED
    assert EvidenceClassification.from_str("conflicting") == EvidenceClassification.CONFLICTING
    assert EvidenceClassification.from_str("contradictory") == EvidenceClassification.CONFLICTING
    assert EvidenceClassification.from_str("unverified") == EvidenceClassification.UNVERIFIED
    assert EvidenceClassification.from_str("unknown_junk") == EvidenceClassification.UNVERIFIED


def test_research_knowledge_package_assembly_and_methods():
    """Build a complete knowledge package and verify claim-source relationships."""
    topic = ResearchTopic(
        title="Quantum Computing Fault Tolerance",
        objective="Analyze surface code progress and logical qubit physical thresholds.",
        target_format="docx",
        subtopics=[
            ResearchSubtopic(
                subtopic_id="sub_surface_codes",
                title="Surface Code Thresholds",
                queries=["surface code error threshold latest 2026", "physical qubit overhead per logical qubit"],
                status="in_progress",
            ),
            ResearchSubtopic(
                subtopic_id="sub_commercial_roadmaps",
                title="Industry Roadmaps",
                queries=["ibm quantum fault tolerance roadmap", "google quantum ai willow roadmap"],
                status="pending",
            ),
        ],
        keywords=["fault tolerance", "surface code", "logical qubits", "error correction"],
    )

    pkg = ResearchKnowledgePackage(package_id="pkg_qc_001", topic=topic)

    # 1. Add Sources
    s1 = SourceReference(
        source_id="src_nature_2026",
        url="https://nature.com/articles/s41586-026-0001",
        domain="nature.com",
        title="Breakthrough in Surface Code Physical Error Suppression",
        author="Quantum Hardware Group",
        publication_date="2026-02-15",
        reliability_score=0.98,
        is_primary=True,
    )
    s2 = SourceReference(
        source_id="src_arxiv_2026",
        url="https://arxiv.org/abs/2601.12345",
        domain="arxiv.org",
        title="Cross-Platform Benchmarking of Fault-Tolerant Architectures",
        author="Theory Consortium",
        publication_date="2026-01-28",
        reliability_score=0.92,
        is_primary=False,
    )
    s3 = SourceReference(
        source_id="src_blog_disputed",
        url="https://tech-speculator.blog/quantum-claims",
        domain="tech-speculator.blog",
        title="10,000 Logical Qubits Tomorrow",
        author="Anonymous",
        reliability_score=0.35,
        is_primary=False,
    )
    pkg.add_source(s1)
    pkg.add_source(s2)
    pkg.add_source(s3)

    # 2. Add Evidence items
    ev1 = EvidenceItem(
        evidence_id="ev_001",
        source_id="src_nature_2026",
        quote="Physical two-qubit error rates achieved 0.08%, well below the 1.0% surface code fault-tolerance threshold.",
        context="Results Section, Paragraph 3",
        location="h2#results",
        confidence=0.99,
    )
    ev2 = EvidenceItem(
        evidence_id="ev_002",
        source_id="src_arxiv_2026",
        quote="Independent calibration confirmed two-qubit gate fidelities exceeding 99.91% across the 72-qubit planar lattice.",
        context="Experimental Verification Section",
        location="section#benchmarks",
        confidence=0.95,
    )
    ev3 = EvidenceItem(
        evidence_id="ev_003",
        source_id="src_blog_disputed",
        quote="Consumer-grade quantum laptops will operate room-temperature logical processors by Q3 2026.",
        context="Introduction",
        confidence=0.20,
    )
    pkg.add_evidence(ev1)
    pkg.add_evidence(ev2)
    pkg.add_evidence(ev3)

    # 3. Add Claims
    clm_corroborated = ResearchClaim(
        claim_id="clm_001",
        statement="Physical two-qubit error rates have breached the sub-0.1% threshold required for viable surface code scaling.",
        classification=EvidenceClassification.CORROBORATED,
        subtopic_id="sub_surface_codes",
        primary_source_id="src_nature_2026",
        supporting_source_ids=["src_arxiv_2026"],
        evidence_item_ids=["ev_001", "ev_002"],
        confidence=0.97,
    )
    clm_conflicting = ResearchClaim(
        claim_id="clm_002",
        statement="Room-temperature quantum processing is commercialized in 2026.",
        classification=EvidenceClassification.CONFLICTING,
        subtopic_id="sub_commercial_roadmaps",
        primary_source_id="src_blog_disputed",
        supporting_source_ids=[],
        evidence_item_ids=["ev_003"],
        counter_evidence_ids=["ev_001", "ev_002"],
        contradiction_notes="All peer-reviewed evidence (Nature, arXiv) confirms millikelvin dilution refrigeration remains essential.",
        confidence=0.10,
    )
    pkg.add_claim(clm_corroborated)
    pkg.add_claim(clm_conflicting)

    # 4. Add Data Table
    tbl = DataTable(
        table_id="tbl_thresholds",
        title="Surface Code Error Threshold Comparison",
        headers=["Code Family", "Physical Error Threshold", "Qubit Overhead (d=7)", "Status"],
        rows=[
            ["Planar Surface Code", "1.0%", "98 qubits", "Experimental"],
            ["X-Cube Color Code", "0.75%", "147 qubits", "Theoretical"],
            ["Heavy-Hex Code", "0.45%", "133 qubits", "Commercial Prototype"],
        ],
        source_ids=["src_nature_2026", "src_arxiv_2026"],
        subtopic_id="sub_surface_codes",
    )
    pkg.add_table(tbl)

    # 5. Add Visual Asset
    vis = VisualAsset(
        asset_id="vis_lattice",
        asset_type="diagram",
        title="Rotated Surface Code Lattice Layout",
        caption="Data and syndrome measurement qubit layout in a rotated surface code.",
        source_url="https://nature.com/articles/s41586-026-0001/figures/1",
        source_id="src_nature_2026",
        attribution="Nature Publishing Group / Authors",
        is_generated=False,
        subtopic_id="sub_surface_codes",
    )
    pkg.add_visual(vis)

    # 6. Add Formula
    form = FormulaItem(
        formula_id="eq_logical_error",
        name="Logical Error Rate Scaling",
        latex=r"P_L \approx C \left(\frac{p}{p_{th}}\right)^{\frac{d+1}{2}}",
        explanation="Exponential suppression of logical error rate with code distance d below threshold p_th.",
        variables={"P_L": "Logical error rate", "p": "Physical error rate", "p_{th}": "Fault-tolerance threshold", "d": "Code distance"},
        subtopic_id="sub_surface_codes",
    )
    pkg.add_formula(form)

    # Verify query methods
    assert len(pkg.sources) == 3
    assert len(pkg.evidence) == 3
    assert len(pkg.claims) == 2
    assert len(pkg.tables) == 1
    assert len(pkg.visuals) == 1
    assert len(pkg.formulas) == 1

    corroborated = pkg.get_corroborated_claims()
    assert len(corroborated) == 1
    assert corroborated[0].claim_id == "clm_001"

    conflicting = pkg.get_conflicting_claims()
    assert len(conflicting) == 1
    assert conflicting[0].claim_id == "clm_002"
    assert "Nature" in conflicting[0].contradiction_notes

    claims_for_nature = pkg.get_claims_for_source("src_nature_2026")
    assert len(claims_for_nature) == 1
    assert claims_for_nature[0].claim_id == "clm_001"

    claims_for_arxiv = pkg.get_claims_for_source("src_arxiv_2026")
    assert len(claims_for_arxiv) == 1
    assert claims_for_arxiv[0].claim_id == "clm_001"


def test_json_serialization_deserialization_fidelity():
    """Verify 100% round-trip fidelity through JSON and Dict serialization."""
    topic = ResearchTopic(
        title="Microservices vs Monoliths Benchmark",
        objective="Compare latency, operational cost, and developer velocity.",
        target_format="pdf",
    )
    pkg = ResearchKnowledgePackage(package_id="pkg_bench_002", topic=topic)
    pkg.add_source(SourceReference(source_id="s1", url="https://martinfowler.com/microservices", domain="martinfowler.com"))
    pkg.add_evidence(EvidenceItem(evidence_id="e1", source_id="s1", quote="Don't start with a microservices architecture."))
    pkg.add_claim(ResearchClaim(
        claim_id="c1",
        statement="Premature microservice decomposition degrades developer velocity.",
        classification=EvidenceClassification.SINGLE_SOURCE,
        primary_source_id="s1",
        evidence_item_ids=["e1"],
    ))
    pkg.add_table(DataTable(
        table_id="t1",
        title="Cost Matrix",
        headers=["Model", "Infra Cost", "Dev Cost"],
        rows=[["Monolith", "$500/mo", "$10,000/mo"], ["Microservices", "$2,500/mo", "$25,000/mo"]],
    ))
    pkg.add_formula(FormulaItem(
        formula_id="f1",
        name="Little's Law",
        latex="L = \\lambda W",
        variables={"L": "Average items in system", "\\lambda": "Arrival rate", "W": "Average wait time"},
    ))

    # JSON Round-trip
    json_str = pkg.to_json()
    assert isinstance(json_str, str)
    rehydrated_from_json = ResearchKnowledgePackage.from_json(json_str)

    assert rehydrated_from_json.package_id == "pkg_bench_002"
    assert rehydrated_from_json.topic.title == "Microservices vs Monoliths Benchmark"
    assert rehydrated_from_json.topic.target_format == "pdf"
    assert "s1" in rehydrated_from_json.sources
    assert rehydrated_from_json.sources["s1"].url == "https://martinfowler.com/microservices"
    assert rehydrated_from_json.claims["c1"].statement == "Premature microservice decomposition degrades developer velocity."
    assert rehydrated_from_json.claims["c1"].classification == EvidenceClassification.SINGLE_SOURCE
    assert rehydrated_from_json.tables[0].headers == ["Model", "Infra Cost", "Dev Cost"]
    assert rehydrated_from_json.formulas[0].latex == "L = \\lambda W"

    # Dict Round-trip
    data_dict = pkg.to_dict()
    assert isinstance(data_dict, dict)
    rehydrated_from_dict = ResearchKnowledgePackage.from_dict(data_dict)
    assert rehydrated_from_dict.package_id == pkg.package_id
    assert rehydrated_from_dict.claims["c1"].claim_id == "c1"


# ---------------------------------------------------------------------------
# 2. Research Intent & Requested Output Format Detection
# ---------------------------------------------------------------------------

def test_detect_requested_format():
    """Verify accurate extraction of all supported document and presentation formats."""
    assert detect_requested_format("Generate a detailed report in PDF format") == "pdf"
    assert detect_requested_format("Explain transformer architecture with diagrams in PDF.") == "pdf"
    assert detect_requested_format("Export findings as a PDF") == "pdf"
    assert detect_requested_format("Prepare a research document in Word docx") == "docx"
    assert detect_requested_format("Save as word doc") == "docx"
    assert detect_requested_format("Prepare a presentation about renewable energy") == "pptx"
    assert detect_requested_format("Create a slide deck for the board") == "pptx"
    assert detect_requested_format("Compare databases in Excel.") == "xlsx"
    assert detect_requested_format("Export comparison spreadsheet") == "xlsx"
    assert detect_requested_format("Generate a CSV table of all specs") == "csv"
    assert detect_requested_format("Write technical notes in Markdown") == "markdown"
    assert detect_requested_format("Save as .md file") == "markdown"
    assert detect_requested_format("Build an interactive HTML report") == "html"
    assert detect_requested_format("What is the speed of light?") is None


def test_detect_deliverable_type():
    """Verify classification of deliverable archetypes."""
    assert detect_deliverable_type("Prepare a presentation on battery tech") == "presentation"
    assert detect_deliverable_type("Create a slide deck about AI") == "presentation"
    assert detect_deliverable_type("Create an Excel comparison") == "dataset"
    assert detect_deliverable_type("Make a comparison spreadsheet") == "dataset"
    assert detect_deliverable_type("Prepare a detailed research report on microservices") == "report"
    assert detect_deliverable_type("Write a whitepaper on cryptography") == "report"
    assert detect_deliverable_type("Hello there") is None


def test_extract_research_topic():
    """Verify extraction of the core inquiry subject from verbose instructions."""
    assert extract_research_topic("Research the latest developments in quantum computing and prepare a detailed report.") == "the latest developments in quantum computing"
    assert extract_research_topic("Explain transformer architecture with diagrams in PDF.") == "transformer architecture"
    assert extract_research_topic("Compare databases in Excel.") == "databases"
    assert extract_research_topic("Prepare a presentation about renewable energy.") == "renewable energy"
    assert extract_research_topic("Research renewable energy technologies and prepare a presentation.") == "renewable energy technologies"
    assert extract_research_topic("Prepare a detailed research report on microservices architecture with relevant diagrams and references.") == "microservices architecture"


# ---------------------------------------------------------------------------
# 3. Guarding Specific User Prompts from Conversational Chat Traps
# ---------------------------------------------------------------------------

def test_user_research_prompts_route_to_research_task_not_conversational():
    """All 4 explicit user prompts must route to RESEARCH_TASK with is_browser=True."""

    # Prompt 1: "Research quantum computing and prepare a report."
    p1 = "Research quantum computing and prepare a report."
    r1 = classify_intent(p1)
    assert r1.intent == TaskIntent.RESEARCH_TASK, f"Expected RESEARCH_TASK, got {r1.intent}"
    assert r1.is_browser is True
    assert r1.is_conversational is False
    assert r1.is_research is True
    assert r1.requested_format == "docx"
    assert r1.deliverable_type == "report"
    assert "quantum computing" in (r1.research_topic or "").lower()

    # Prompt 2: "Explain transformer architecture with diagrams in PDF."
    p2 = "Explain transformer architecture with diagrams in PDF."
    r2 = classify_intent(p2)
    assert r2.intent == TaskIntent.RESEARCH_TASK, f"Expected RESEARCH_TASK, got {r2.intent}"
    assert r2.is_browser is True
    assert r2.is_conversational is False
    assert r2.is_research is True
    assert r2.requested_format == "pdf"
    assert "transformer architecture" in (r2.research_topic or "").lower()

    # Prompt 3: "Compare databases in Excel."
    p3 = "Compare databases in Excel."
    r3 = classify_intent(p3)
    assert r3.intent == TaskIntent.RESEARCH_TASK, f"Expected RESEARCH_TASK, got {r3.intent}"
    assert r3.is_browser is True
    assert r3.is_conversational is False
    assert r3.is_research is True
    assert r3.requested_format == "xlsx"
    assert r3.deliverable_type == "dataset" or r3.deliverable_type == "comparison" or "database" in (r3.research_topic or "")

    # Prompt 4: "Prepare a presentation about renewable energy."
    p4 = "Prepare a presentation about renewable energy."
    r4 = classify_intent(p4)
    assert r4.intent == TaskIntent.RESEARCH_TASK, f"Expected RESEARCH_TASK, got {r4.intent}"
    assert r4.is_browser is True
    assert r4.is_conversational is False
    assert r4.is_research is True
    assert r4.requested_format == "pptx"
    assert r4.deliverable_type == "presentation"
    assert "renewable energy" in (r4.research_topic or "").lower()


def test_detailed_prompt_variations_route_to_research():
    """Verify long-form variants from the vision document."""
    prompts = [
        ("Research the latest developments in quantum computing and prepare a detailed report.", "docx", "report"),
        ("Compare the leading database technologies and create an Excel comparison.", "xlsx", "dataset"),
        ("Explain transformer architecture with diagrams, equations and examples in a PDF.", "pdf", None),
        ("Research renewable energy technologies and prepare a presentation.", "pptx", "presentation"),
        ("Prepare a detailed research report on microservices architecture with relevant diagrams and references.", "docx", "report"),
    ]
    for prompt, expected_fmt, expected_deliv in prompts:
        res = classify_intent(prompt)
        assert res.intent == TaskIntent.RESEARCH_TASK, f"Failed for: {prompt}"
        assert res.is_browser is True
        assert res.is_research is True
        assert res.requested_format == expected_fmt, f"Expected format {expected_fmt}, got {res.requested_format}"
        if expected_deliv:
            assert res.deliverable_type == expected_deliv


# ---------------------------------------------------------------------------
# 4. Preservation of Existing Non-Browser, Browser, and Continuation Behaviors
# ---------------------------------------------------------------------------

def test_preserves_pure_conversational_questions():
    """Pure conceptual questions without deliverable artifact requests must remain NON_BROWSER."""
    pure_conversational = [
        "Explain transformers",
        "What is machine learning?",
        "Define polymorphism",
        "How does recursion work?",
        "Write a C++ BFS program",
        "Calculate 125 * 8",
        "Calculate 25% of 800",
        "Hi",
        "Hello!",
        "hi man",
        "Who are you?",
    ]
    for prompt in pure_conversational:
        res = classify_intent(prompt)
        assert res.intent == TaskIntent.NON_BROWSER, f"Expected NON_BROWSER for '{prompt}', got {res.intent}"
        assert res.is_browser is False
        assert res.is_conversational is True
        assert res.is_research is False


def test_preserves_standard_browser_and_email_tasks():
    """Existing navigation, search, and email commands must retain NEW_BROWSER_TASK."""
    standard_browser = [
        "Go to amazon.in and search for laptops",
        "https://en.wikipedia.org/wiki/Artificial_intelligence",
        "Navigate to google.com",
        "Search for mechanical keyboard on flipkart",
        "Look up flight prices on kayak.com",
        "open mail",
        "check my inbox",
    ]
    for prompt in standard_browser:
        res = classify_intent(prompt)
        assert res.intent == TaskIntent.NEW_BROWSER_TASK, f"Expected NEW_BROWSER_TASK for '{prompt}', got {res.intent}"
        assert res.is_browser is True


def test_preserves_continuation_and_user_tab_intent():
    """Continuation with active session and explicit user-tab attachment must remain intact."""
    fake_session = BrowserSession(
        session_id="sess_123",
        task_id="task_123",
        conversation_id="conv_123",
        origin="new_task",
        primary_tab_id=1,
    )

    # Continuation
    res_cont = classify_intent("now click on the first result", context={"active_session": fake_session})
    assert res_cont.intent == TaskIntent.CONTINUE_BROWSER_TASK
    assert res_cont.is_browser is True

    # Continuation of research
    res_cont_research = classify_intent("continue researching and add findings to the report", context={"active_session": fake_session})
    assert res_cont_research.intent == TaskIntent.CONTINUE_BROWSER_TASK
    assert res_cont_research.is_browser is True
    assert res_cont_research.is_research is True
    assert res_cont_research.requested_format == "docx"

    # User Tab
    res_tab = classify_intent("use my active tab")
    assert res_tab.intent == TaskIntent.USE_EXISTING_USER_TAB
    assert res_tab.is_browser is True

