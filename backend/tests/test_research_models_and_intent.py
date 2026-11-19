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
    ResearchOperationType,
    TaskIntent,
    classify_intent,
    cut_delivery_clauses,
    detect_deliverable_type,
    detect_requested_format,
    extract_research_topic,
    has_unresolved_reference,
    is_export_deliverable_directive,
    is_research_or_deliverable_request,
    split_compound_instruction,
    thread_compound_context,
    update_compound_context,
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
    # "xcel" is the common misspelling of Excel (live price-hunt
    # prompt: "give the detailed comparison in a xcel").
    assert detect_requested_format("give the detailed comparison in a xcel") == "xlsx"
    assert detect_requested_format("Generate a CSV table of all specs") == "csv"
    assert detect_requested_format("Write technical notes in Markdown") == "markdown"
    assert detect_requested_format("Save as .md file") == "markdown"
    assert detect_requested_format("Build an interactive HTML report") == "html"
    assert detect_requested_format("What is the speed of light?") is None


def test_detect_requested_format_bare_ppt():
    """Bare "ppt" is how users actually say it — it must read as pptx.

    Live 2026-11-19: "ppt report on topic university should teach beyond
    classical syllabus in this AI Era. use images and make it nice looking
    ppt" returned None, so the request never reached the research pipeline
    and the agent hallucinated a `createpresentation` tool call instead.
    FORMAT_PPTX_RE matched pptx/powerpoint/presentation/slides but not the
    bare word "ppt" (compare FORMAT_XLSX_RE, which already has the "xcel"
    alias for exactly this reason).
    """
    assert detect_requested_format("ppt report on topic black holes") == "pptx"
    assert detect_requested_format("make a ppt about black holes") == "pptx"
    assert detect_requested_format("a nice looking ppt on black holes") == "pptx"
    assert detect_requested_format(
        "ppt report on topic university should teach beyond classical syllabus "
        "in this AI Era. use images and make it nice looking ppt"
    ) == "pptx"


def test_detect_deliverable_type_bare_ppt():
    """A bare "ppt" deliverable noun is a presentation, not a report."""
    assert detect_deliverable_type("ppt report on topic black holes") == "presentation"
    assert detect_deliverable_type("make a ppt about black holes") == "presentation"


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


def test_extract_research_topic_strips_bare_ppt_preamble():
    """A bare leading deliverable preamble is not the topic.

    Live 2026-11-19: extract_research_topic("ppt report on topic
    university should teach...") returned the whole instruction with
    "ppt on topic" still leading, so the research pipeline (had it been
    reached) would have searched for the preamble instead of the subject.
    """
    assert extract_research_topic(
        "ppt report on topic university should teach beyond classical syllabus "
        "in this AI Era. use images and make it nice looking ppt"
    ) == "university should teach beyond classical syllabus in this AI Era"
    assert extract_research_topic("ppt on black holes") == "black holes"
    assert extract_research_topic("presentation on black holes") == "black holes"
    # A navigation preamble must only strip a NAMED engine: "open source
    # licensing" is a topic, not "open google".
    assert extract_research_topic("research open source licensing") == "open source licensing"


def test_ppt_report_bare_noun_phrase_routes_to_research_task():
    """"ppt report on topic X" is a research + pptx request, not chat.

    Live 2026-11-19: this exact instruction classified NON_BROWSER
    (0.8, "general knowledge/conversational request"), so it never
    entered the research pipeline; the LLM answered directly and
    hallucinated a `createpresentation` tool call, and no file was ever
    produced. A bare deliverable noun phrase with a topic must route to
    RESEARCH_TASK with format pptx.
    """
    instruction = (
        "ppt report on topic university should teach beyond classical syllabus "
        "in this AI Era. use images and make it nice looking ppt"
    )
    cl = classify_intent(instruction)
    assert cl.intent == TaskIntent.RESEARCH_TASK, f"Expected RESEARCH_TASK, got {cl.intent}"
    assert cl.is_research is True
    assert cl.requested_format == "pptx"
    assert cl.deliverable_type == "presentation"
    assert cl.research_topic == "university should teach beyond classical syllabus in this AI Era"


def test_ppt_report_with_leading_filler_routes_to_research_task():
    """Review #4, M3: "please ppt report on topic X" must route too.

    The bare-preamble regex is ^-anchored; with no leading-filler
    allowance the most natural phrasing ("please ...") reproduced the
    live failure — NON_BROWSER, no pipeline, no file.
    """
    for prefix in ("please ", "hey, ", "hi ", "ok, "):
        instruction = prefix + "ppt report on topic quantum computing"
        cl = classify_intent(instruction)
        assert cl.intent == TaskIntent.RESEARCH_TASK, f"{instruction!r} -> {cl.intent}"
        assert cl.requested_format == "pptx"
        assert cl.research_topic == "quantum computing"


def test_messaging_send_routes_to_browser_task_not_chat():
    """Review #4, M1: a send to a named recipient on a channel is a
    browser task. The NON_BROWSER fast-path has no messaging handler,
    so without this rule a send "completes" by chatting and nothing is
    ever sent (live 2026-11-19).
    """
    for instruction in (
        "send it to prateek in whatsapp",
        "send 'babua' to Mummy on WhatsApp",
        "send the summary below to prateek in whatsapp",
        "ask a random question to Prateek on whatsapp",
        "message Prateek on whatsapp",
        "share the link with prateek on whatsapp",
    ):
        cl = classify_intent(instruction)
        assert cl.intent == TaskIntent.NEW_BROWSER_TASK, f"{instruction!r} -> {cl.intent}"
        assert cl.is_browser is True
    # "send it to me by email" stays with the email-dispatch handler,
    # not the messaging rule (the rule's prepositions exclude by/for).
    cl = classify_intent("send it to me by email")
    assert cl.intent != TaskIntent.NEW_BROWSER_TASK or "email" in (cl.reason or "").lower()
    # Review #5, M6/M7: month/time words and email words are NOT
    # channels — "in June" is timing, "on email" is the email handler.
    # All four M6 inputs and both M7 inputs were NEW_BROWSER_TASK
    # before the stoplist (a regression vs base, proven by stash A/B).
    for instruction in (
        "send the invoice to Priya in June",
        "send the report to Ana in May",
        "send it to Rahul in the morning",
        "message Rahul in June about the meeting",
        "send the report to Prateek on email",
        "share the report with Prateek via email",
    ):
        assert classify_intent(instruction).intent == TaskIntent.NON_BROWSER, instruction
    # A bare "ask" with no recipient/channel is still just chat.
    assert classify_intent("ask a random question").intent == TaskIntent.NON_BROWSER


def test_compound_channel_not_set_by_month_words():
    """Review #5, M6: the threading channel extractor shares the
    classifier's stoplist — "send it to prateek in june" must not
    record "june" as the channel a later part inherits.
    """
    assert update_compound_context(
        {}, "send it to prateek in june", ("NEW_BROWSER_TASK", "completed", "sent")
    ).get("last_channel") is None
    assert update_compound_context(
        {}, "send it to prateek in whatsapp", ("NEW_BROWSER_TASK", "completed", "sent")
    ).get("last_channel") == "whatsapp"


def test_send_me_ppt_for_review_is_not_research():
    """Review #5, S7: "send me the ppt for review" asks for an existing
    file, not new research — at base it was NON_BROWSER; the batch
    routed it RESEARCH_TASK with the mangled topic "send me the for
    review". With a real subject the deliverable route stands.
    """
    assert classify_intent("send me the ppt for review").intent == TaskIntent.NON_BROWSER
    assert classify_intent("send me the ppt").intent == TaskIntent.NON_BROWSER
    assert extract_research_topic("send me the ppt for review") is None
    cl = classify_intent("send me the ppt about quantum computing")
    assert cl.intent == TaskIntent.RESEARCH_TASK
    assert cl.research_topic == "quantum computing"
    cl = classify_intent("give me the ppt about quantum computing")
    assert cl.intent == TaskIntent.RESEARCH_TASK
    assert cl.research_topic == "quantum computing"


def test_delivery_clause_cut_keeps_topical_send_phrases():
    """Review #4, M5: the cut must not eat a research topic that happens
    to contain "send ... to ...".

    "research how to send money to friends" is research about
    money-transfer methods, not a delivery clause; "send me the ppt for
    review" has no destination clause, so it must not cut to empty.
    """
    assert cut_delivery_clauses("research how to send money to friends") == (
        "research how to send money to friends"
    )
    assert extract_research_topic("research how to send money to friends") == (
        "how to send money to friends"
    )
    assert cut_delivery_clauses("send me the ppt for review") == "send me the ppt for review"
    # Review #5, S7: the M5-era assertion here ("send me the for
    # review") was a garbage topic — the delivery prefix and the
    # purpose clause are now stripped, so the fragment has no subject
    # and extracts to None instead of a nonsense topic.
    assert extract_research_topic("send me the ppt for review") is None
    assert extract_research_topic("send me the ppt about quantum computing") == (
        "quantum computing"
    )
    assert extract_research_topic("give me the ppt about quantum computing") == (
        "quantum computing"
    )
    # The live delivery clauses still cut.
    assert cut_delivery_clauses(
        "research about black holes and send it to prateek in whatsapp"
    ) == "research about black holes"
    assert cut_delivery_clauses("research X and send the summary to prateek") == "research X"


def test_compound_instruction_research_topic_is_research_half_only():
    """Delivery/messaging/shopping clauses are not part of the research topic.

    Live 2026-11-19: classify_intent on the compound black-holes /
    WhatsApp / Amazon instruction returned RESEARCH_TASK with
    research_topic = the ENTIRE instruction, and decompose turned it
    into garbage queries ("google about black holes and send to
    prateek"). The research topic must be the research clause only.
    """
    instruction = (
        "open google, research about black holes and send it to prateek in "
        "whatsapp, also ask a random question to him, and then open amazon "
        "and send redmi phone link to prateek"
    )
    assert extract_research_topic(instruction) == "black holes"


def test_compound_splitter_four_parts_and_idempotent():
    """The live compound instruction splits into four runnable parts.

    Live 2026-11-19: the whole instruction ran as ONE research mission
    with a poisoned topic; the WhatsApp send, the question, and the
    Amazon link send never executed. The splitter must produce the
    four parts (bare "open <site>" folds into the following part), and
    splitting any part must return just that part (the runner recurses
    into run_task, which re-splits — a non-idempotent splitter would
    loop or re-split forever).
    """
    instruction = (
        "open google, research about black holes and send it to prateek in "
        "whatsapp, also ask a random question to him, and then open amazon "
        "and send redmi phone link to prateek"
    )
    parts = split_compound_instruction(instruction)
    assert parts == [
        "open google, research about black holes",
        "send it to prateek in whatsapp",
        "ask a random question to him",
        "open amazon, send redmi phone link to prateek",
    ]
    for part in parts:
        assert split_compound_instruction(part) == [part], f"not idempotent: {part!r}"


def test_compound_splitter_does_not_split_singles_or_owned_compounds():
    """No false splits: topics with "and", owned compounds, PPT phrase."""
    assert split_compound_instruction("research whatsapp privacy policy") == [
        "research whatsapp privacy policy"
    ]
    # Email+calendar / email-mutation compounds stay one mission: the
    # mission-control dispatcher and the email+calendar handler own them.
    assert split_compound_instruction("check my email and delete promo emails") == [
        "check my email and delete promo emails"
    ]
    assert split_compound_instruction("research Tom and Jerry") == ["research Tom and Jerry"]
    assert split_compound_instruction("explain black holes") == ["explain black holes"]
    # Review #4, S2: ordinary multi-clause instructions must NOT split —
    # the second clause runs on the first clause's findings in one
    # mission; splitting it runs blind.
    for single in (
        "search for flights and check prices",
        "check my email and check my calendar",
        "research quantum computing and tell me more",
        "check the weather and go for a walk",
    ):
        assert split_compound_instruction(single) == [single], single
    # Review #5, S8: an "open" clause that refers back to the previous
    # part's findings ("the cheapest one", "the first result") is not
    # a new sub-task — it has no site or object of its own.
    for single in (
        "search for flights and open the cheapest one",
        "search for flights and open the first result",
        "search for flights and open it",
    ):
        assert split_compound_instruction(single) == [single], single
    # But a self-contained "open" clause still splits.
    assert split_compound_instruction("research black holes and open amazon") == [
        "research black holes",
        "open amazon",
    ]
    assert split_compound_instruction(
        "ppt report on topic university should teach beyond classical syllabus "
        "in this AI Era. use images and make it nice looking ppt"
    ) == [
        "ppt report on topic university should teach beyond classical syllabus "
        "in this AI Era. use images and make it nice looking ppt"
    ]


def test_compound_threading_resolves_pronouns_from_earlier_parts():
    """'it' becomes the summary, 'him' the named recipient, and a
    messaging part without a channel inherits the last one.

    Review #4, M1/M2: the threading wording is trigger-free ("the
    summary below" / "Summary:" — the word "research" must not appear,
    or the threaded part misroutes to a garbage research run), and the
    replacements run on the part text BEFORE the summary block is
    appended, so a summary containing "them" is never rewritten.
    """
    ctx = {
        "research_summary": "Black holes are regions where gravity is so strong that nothing can escape them.",
        "last_recipient": "Prateek",
        "last_channel": "whatsapp",
    }
    threaded = thread_compound_context("send it to prateek in whatsapp", ctx)
    assert "Black holes are regions where gravity is so strong that nothing can escape them." in threaded
    assert " the summary below " in threaded
    assert "Research summary:" not in threaded
    assert "research" not in threaded.lower()
    # The threaded send must classify as a browser messaging task, not
    # RESEARCH_TASK with topic None and not a NON_BROWSER fake-complete.
    cl = classify_intent(threaded)
    assert cl.intent == TaskIntent.NEW_BROWSER_TASK, f"threaded send -> {cl.intent}"
    # Part 3 inherits the channel: "ask ... to Prateek" is asked on the
    # same channel as the earlier send.
    threaded3 = thread_compound_context("ask a random question to him", ctx)
    assert threaded3 == "ask a random question to Prateek on whatsapp"
    assert classify_intent(threaded3).intent == TaskIntent.NEW_BROWSER_TASK
    # Part 4 (navigation preamble + send) inherits the channel too.
    threaded4 = thread_compound_context("open amazon, send redmi phone link to prateek", ctx)
    assert threaded4 == "open amazon, send redmi phone link to prateek on whatsapp"
    # Without a summary, "it" is unresolved — the runner skips such a
    # part when an earlier part failed, instead of sending garbage.
    assert has_unresolved_reference("send it to prateek in whatsapp", {}) is True
    assert has_unresolved_reference("ask a random question to him", {}) is False
    # Context update: a completed research part supplies the summary,
    # and the recipient + channel are read from the part text.
    ctx2 = update_compound_context({}, "send it to prateek in whatsapp",
                                   ("NEW_BROWSER_TASK", "completed", "sent"))
    assert ctx2["last_recipient"] == "Prateek"
    assert ctx2["last_channel"] == "whatsapp"
    ctx3 = update_compound_context({}, "research about black holes",
                                   ("RESEARCH_TASK", "completed", "Black holes: ..."))
    assert ctx3["research_summary"] == "Black holes: ..."
    # A non-messaging part never sets a channel ("go to paris in june").
    assert update_compound_context({}, "go to paris in june",
                                   ("NEW_BROWSER_TASK", "completed", "done")).get("last_channel") is None


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



# ---------------------------------------------------------------------------
# 8. Export-directive detection for "give a pdf report" follow-ups
#    (live failure 2026-11-19: "or atleast give a pdf report about
#    this, that should include images related to it too" was not
#    recognised as a pure export, so it became a fresh research task
#    with the garbage query "atleast give about should include images
#    related to", Google returned askfilo.com, and the task died on
#    the domain allow-list.)
# ---------------------------------------------------------------------------

def test_export_directive_pdf_report_followup_phrasings():
    """The reported follow-up and its variants are pure export directives."""
    for text in [
        "or atleast give a pdf report about this, that should include images related to it too",
        "atleast give a pdf report about this, that should include images related to it too",
        "give a pdf report about this, that should include images related to it too",
        "give a pdf report about this",
        "give a pdf report",
        "generate a pdf report",
        "give me a pdf report of the previous research",
        # Pronoun + deliverable noun ("about this topic"): a bare
        # pronoun without its noun left the noun behind and decomposed
        # to the garbage query "research".
        "give a pdf report about this topic",
        "give a pdf report about this research",
        "give a pdf report about that report",
        "give a pdf report on this topic",
        "give a pdf report for this",
        "give a pdf report about this but also add a table",
    ]:
        is_exp, fmt, _ = is_export_deliverable_directive(text)
        assert is_exp is True, text
        assert fmt == "pdf", text


def test_export_directive_pdf_report_routes_to_export_existing_package():
    """With a prior research topic in context, the follow-up compiles
    the PDF from existing research — no new browser search, no garbage
    query, no askfilo.com detour."""
    ctx = {
        "research_topic": "Quantum Mechanics similarities with life",
        "conversation_history": [
            {"role": "user", "content": 'so i have to submit a research paper on topic: "Quantum Mechanics similarities with life?" so can you research about this topic online'}
        ],
    }
    res = classify_intent(
        "or atleast give a pdf report about this, that should include images related to it too",
        context=ctx,
    )
    assert res.intent == TaskIntent.NON_BROWSER
    assert res.operation_type == ResearchOperationType.EXPORT_EXISTING_PACKAGE
    assert res.requested_format == "pdf"
    assert res.research_topic == "Quantum Mechanics similarities with life"


def test_export_directive_explicit_new_topic_is_not_pure_export():
    """An explicit NEW subject ("about Quantum Mechanics") must NOT be
    treated as an export of existing research — it falls through to
    new research + deliverable instead."""
    for text in [
        "give a pdf report about Quantum Mechanics similarities with life",
        "give a pdf report about Quantum Mechanics similarities with life, include images",
        # A pronoun reference followed by an explicit new subject is
        # still a new research task, not an export of existing research.
        "give a pdf report about this topic on quantum biology",
        "give a pdf report about this research on transformers",
        "give a pdf report about that report on batteries",
        "give a pdf report about this but also research quantum biology",
    ]:
        is_exp, _, _ = is_export_deliverable_directive(text)
        assert is_exp is False, text


def test_export_directive_pronoun_noun_followup_never_searches_garbage():
    """Audit find: "give a pdf report about this research" used to
    classify as NEW_RESEARCH and decompose to the garbage query
    "research". It must now route to EXPORT_EXISTING_PACKAGE and
    decompose to no query at all when there is no prior topic."""
    from app.website_resolver import WebsiteResolver

    ctx = {
        "research_topic": "Quantum Mechanics similarities with life",
        "conversation_history": [
            {"role": "user", "content": 'so i have to submit a research paper on topic: "Quantum Mechanics similarities with life?"'}
        ],
    }
    for text in [
        "give a pdf report about this research",
        "give a pdf report about this topic",
        "give a pdf report about this but also add a table",
    ]:
        res = classify_intent(text, context=ctx)
        assert res.intent == TaskIntent.NON_BROWSER, text
        assert res.operation_type == ResearchOperationType.EXPORT_EXISTING_PACKAGE, text
        assert res.requested_format == "pdf", text
        assert WebsiteResolver().decompose_search_queries(text, context={}) == [], text


def test_decompose_pdf_followup_uses_prior_topic_not_garbage():
    """A pdf follow-up about 'this' must decompose to the prior topic,
    never to the scaffolding words. With no prior topic there is no
    search at all (the export handler then says honestly that no prior
    research exists) instead of a garbage Google query."""
    from app.website_resolver import WebsiteResolver

    resolver = WebsiteResolver()
    text = "or atleast give a pdf report about this, that should include images related to it too"

    with_prior = resolver.decompose_search_queries(
        text, context={"research_topic": "Quantum Mechanics similarities with life"}
    )
    assert with_prior, "expected queries for the prior topic"
    assert with_prior[0] == "Quantum Mechanics similarities with life", with_prior
    assert not any("atleast" in q for q in with_prior), with_prior

    without_prior = resolver.decompose_search_queries(text, context={})
    assert without_prior == [], without_prior


def test_export_directive_based_on_till_now_phrasings():
    """\"based on till now / so far\" is a reference to the research done
    so far, not a new subject. A deliverable request built on it
    (\"generate pdf based on research till now\") is a pure export of the
    existing research — it must not become a new research task on the
    literal topic \"based on till now\"."""
    for text in [
        "generate pdf based on research till now",
        "generate pdf based on till now",
        "give me a pdf based on research till now",
        "based on the research till now, generate a pdf",
        "generate a pdf based on the work so far",
        "generate a docx based on till now",
    ]:
        is_exp, fmt, _ = is_export_deliverable_directive(text)
        assert is_exp is True, text
        assert fmt in {"pdf", "docx"}, text


def test_based_on_till_now_resolves_to_prior_topic():
    """A bare \"based on till now / so far\" names no new subject — it
    resolves to the prior research topic from context, in extract,
    classify and decompose. An explicit new subject after \"based on\"
    (\"based on quantum mechanics\") is still a NEW research task, not
    an export of existing research."""
    from app.website_resolver import WebsiteResolver

    ctx = {
        "research_topic": "Quantum Mechanics similarities with life",
        "conversation_history": [
            {"role": "user", "content": 'so i have to submit a research paper on topic: "Quantum Mechanics similarities with life?"'}
        ],
    }
    resolver = WebsiteResolver()
    for text in [
        "generate pdf based on research till now",
        "generate pdf based on till now",
        "based on till now",
        "based on the research till now, generate a pdf",
        "based on so far",
        "till now",
    ]:
        assert extract_research_topic(text, context=ctx) == "Quantum Mechanics similarities with life", text
        queries = resolver.decompose_search_queries(text, context=ctx)
        assert queries and queries[0] == "Quantum Mechanics similarities with life", (text, queries)
        assert not any("till now" in q.lower() or "so far" in q.lower() for q in queries), (text, queries)
    # Export phrasings route to EXPORT_EXISTING_PACKAGE with the prior topic.
    for text in [
        "generate pdf based on research till now",
        "based on the research till now, generate a pdf",
    ]:
        res = classify_intent(text, context=ctx)
        assert res.intent == TaskIntent.NON_BROWSER, text
        assert res.operation_type == ResearchOperationType.EXPORT_EXISTING_PACKAGE, text
        assert res.requested_format == "pdf", text
        assert res.research_topic == "Quantum Mechanics similarities with life", text
    # A bare reference with no prior topic yields no topic and no search.
    assert extract_research_topic("based on till now", context={}) is None
    assert resolver.decompose_search_queries("based on till now", context={}) == []
    # An explicit NEW subject after "based on" is not a reference.
    res = classify_intent("generate pdf based on quantum mechanics", context=ctx)
    assert res.intent == TaskIntent.RESEARCH_TASK, res
    assert res.operation_type == ResearchOperationType.NEW_RESEARCH, res
    assert extract_research_topic("generate pdf based on quantum mechanics", context=ctx) == "based on quantum mechanics"


def test_export_directive_ref_first_with_new_subject_is_new_research():
    """A reference FIRST, then an explicit new subject (\"based on the
    research till now, generate a pdf about quantum mechanics\") is NEW
    research on that subject — not an export of the prior research.
    Before the fix the reference-first export check used re.match
    with no tail constraint, so it returned export=True and the new
    subject was silently dropped (the pdf would have been built on the
    old topic). The topic extractor had the same hole: step 4's
    trailing-clause strip ate \"generate a pdf about quantum
    mechanics\" wholesale, leaving the bare reference, which resolved
    to the prior topic."""
    from app.website_resolver import WebsiteResolver

    ctx = {
        "research_topic": "Quantum Mechanics similarities with life",
        "conversation_history": [
            {"role": "user", "content": 'so i have to submit a research paper on topic: "Quantum Mechanics similarities with life?"'}
        ],
    }
    text = "based on the research till now, generate a pdf about quantum mechanics"
    is_exp, _, _ = is_export_deliverable_directive(text, context=ctx)
    assert is_exp is False, text
    assert extract_research_topic(text, context=ctx) == "quantum mechanics", text
    res = classify_intent(text, context=ctx)
    assert res.intent == TaskIntent.RESEARCH_TASK, res
    assert res.operation_type == ResearchOperationType.NEW_RESEARCH, res
    assert res.research_topic == "quantum mechanics", res
    # The verb may be "give" too ("based on till now, give a pdf
    # about quantum mechanics") — the new-subject check mirrors the
    # export pattern's verb list, not a subset of it.
    assert extract_research_topic(
        "based on till now, give a pdf about quantum mechanics", context=ctx
    ) == "quantum mechanics"
    # And the give variant must CLASSIFY as research too: the format
    # directive's verb list missed "give", so it classified
    # NON_BROWSER while the generate variant yielded NEW_RESEARCH.
    res_give = classify_intent(
        "based on till now, give a pdf about quantum mechanics", context=ctx
    )
    assert res_give.intent == TaskIntent.RESEARCH_TASK, res_give
    assert res_give.operation_type == ResearchOperationType.NEW_RESEARCH, res_give
    assert res_give.research_topic == "quantum mechanics", res_give
    # A requirement tail on the new subject is stripped, not kept
    # ("quantum mechanics with images" -> "quantum mechanics").
    assert extract_research_topic(
        "based on till now, generate a pdf about quantum mechanics with images", context=ctx
    ) == "quantum mechanics"
    # Pure references still export (controls — the fix must not
    # break the ref-first export path).
    for pure in [
        "based on the research till now, generate a pdf",
        "based on till now, generate a pdf",
        "generate pdf based on research till now",
    ]:
        is_exp, fmt, _ = is_export_deliverable_directive(pure, context=ctx)
        assert is_exp is True, pure
        assert fmt == "pdf", pure
        assert extract_research_topic(pure, context=ctx) == "Quantum Mechanics similarities with life", pure


def test_bare_so_far_resolves_to_prior_topic():
    """Bare \"so far\" is a reference to the research done so far.
    Before the fix, extract_research_topic stripped the leading
    \"so\" as a conversational opener, so \"so far\" became \"far\"
    before the reference check ran — the topic was the literal word
    \"far\"."""
    ctx = {
        "research_topic": "Quantum Mechanics similarities with life",
        "conversation_history": [
            {"role": "user", "content": 'research paper on topic: "Quantum Mechanics similarities with life?"'}
        ],
    }
    assert extract_research_topic("so far", context=ctx) == "Quantum Mechanics similarities with life"
    assert extract_research_topic("based on so far", context=ctx) == "Quantum Mechanics similarities with life"
    # An opener in front of a bare reference ("yeah, so far", "ok, so
    # far") is still just the reference: step 1 strips the opener AND
    # "so" (also an opener), leaving the nonsense topic "far".
    for text in ["yeah, so far", "ok, so far", "so, far", "yeah, till now", "well, so far"]:
        assert extract_research_topic(text, context=ctx) == "Quantum Mechanics similarities with life", text
    from app.website_resolver import WebsiteResolver

    queries = WebsiteResolver().decompose_search_queries("so far", context=ctx)
    assert queries and queries[0] == "Quantum Mechanics similarities with life", queries


def test_mixed_reference_plus_modifier_strips_reference():
    """\"based on till now, and also compare with biology\" is the prior
    topic plus a modifier — the literal reference phrase must not
    survive into the topic (\"... - based on till now, and also
    compare with biology\" was the pre-fix topic)."""
    ctx = {
        "research_topic": "Quantum Mechanics similarities with life",
        "conversation_history": [
            {"role": "user", "content": 'research paper on topic: "Quantum Mechanics similarities with life?"'}
        ],
    }
    assert extract_research_topic(
        "based on till now, and also compare with biology", context=ctx
    ) == "Quantum Mechanics similarities with life - compare with biology"
    # And the query decomposition must use the same resolved topic —
    # the literal reference phrase must not become the query with the
    # modifier ("biology") dropped.
    from app.website_resolver import WebsiteResolver

    queries = WebsiteResolver().decompose_search_queries(
        "based on till now, and also compare with biology", context=ctx
    )
    assert queries, queries
    assert any("biology" in q.lower() for q in queries), queries
    assert not any("till now" in q.lower() for q in queries), queries


def test_no_trailing_comma_in_topic():
    """\"based on quantum mechanics, generate a pdf\" must not leave a
    trailing comma on the topic (pre-fix topic ended with \",\")."""
    ctx = {
        "research_topic": "Quantum Mechanics similarities with life",
        "conversation_history": [
            {"role": "user", "content": 'research paper on topic: "Quantum Mechanics similarities with life?"'}
        ],
    }
    topic = extract_research_topic("based on quantum mechanics, generate a pdf", context=ctx)
    assert topic == "based on quantum mechanics", topic


def test_yeah_research_about_the_topic_first_resolves_prior_topic():
    """\"yeah research about the topic first\" is an anaphoric follow-up
    on the prior topic (the PPT chat: prior turn asked for a ppt on
    \"university should teach beyond classical syllabus in this AI
    Era\"). Before the fix, \"yeah\" was not in the opener-strip list
    and \"the topic\" was never resolved, so the literal topic became
    \"yeah first\" / \"the topic first\" and decomposed into garbage
    queries (\"yeah first overview technical principles\",
    \"yeah about the topic first technical mechanics implementation
    methods\")."""
    from app.website_resolver import WebsiteResolver

    prior = "university should teach beyond classical syllabus in this AI Era"
    ctx = {
        "research_topic": prior,
        "conversation_history": [
            {"role": "user", "content": "ppt report on topic university should teach beyond classical syllabus in this AI Era. use images and make it nice looking ppt"}
        ],
    }
    text = "yeah research about the topic first"
    assert extract_research_topic(text, context=ctx) == prior, text
    queries = WebsiteResolver().decompose_search_queries(text, context=ctx)
    assert queries, queries
    assert all("yeah" not in q.lower() and "first" not in q.lower() for q in queries), queries
    assert any(prior.split(" in ")[0].lower() in q.lower() for q in queries), queries
    # The comma variant ("yeah, research about the topic first") must
    # resolve the same way — the opener strip used to require
    # whitespace after the opener word, so "yeah," survived and became
    # the literal topic "yeah, about the topic first".
    assert extract_research_topic("yeah, research about the topic first", context=ctx) == prior
    queries = WebsiteResolver().decompose_search_queries(
        "yeah, research about the topic first", context=ctx
    )
    assert queries and all("yeah" not in q.lower() for q in queries), queries
