"""Unit tests for Phase 4.3: Research Persistence, Checkpoint Resumability & Artifact Registry."""
from __future__ import annotations

import tempfile
import time
from pathlib import Path

from app import db
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


def _create_sample_package() -> ResearchKnowledgePackage:
    topic = ResearchTopic(
        title="Solid-State Battery Breakthroughs",
        objective="Investigate energy density and commercialization timelines",
        target_format="docx",
        subtopics=[
            ResearchSubtopic(
                subtopic_id="sub_001",
                title="Electrolyte Chemistry",
                queries=["solid-state battery electrolyte 2024"],
                status="completed",
                findings_summary="Sulfide-based electrolytes show high ionic conductivity.",
                claim_ids=["clm_001"],
            )
        ],
    )
    pkg = ResearchKnowledgePackage(
        package_id="pkg_test_001",
        topic=topic,
    )
    src = SourceReference(
        source_id="src_001",
        url="https://nature.com/articles/s41586-024-test",
        domain="nature.com",
        title="Advances in Sulfide Solid Electrolytes",
        author="Dr. Jane Doe",
        publication_date="2024-03-15",
        reliability_score=0.98,
        is_primary=True,
    )
    pkg.add_source(src)
    ev = EvidenceItem(
        evidence_id="ev_001",
        source_id="src_001",
        quote="Ionic conductivity reached 25 mS/cm at 25°C.",
        confidence=0.95,
    )
    pkg.add_evidence(ev)
    clm = ResearchClaim(
        claim_id="clm_001",
        statement="Sulfide electrolytes demonstrated 25 mS/cm ionic conductivity at room temperature.",
        classification=EvidenceClassification.SINGLE_SOURCE,
        claim_type="statistic",
        primary_source_id="src_001",
        evidence_item_ids=["ev_001"],
        confidence=0.93,
    )
    pkg.add_claim(clm)
    tbl = DataTable(
        table_id="tbl_001",
        title="Electrolyte Comparison",
        headers=["Type", "Conductivity (mS/cm)", "Stability"],
        rows=[["Sulfide", "25", "Moderate"], ["Oxide", "1.2", "High"]],
    )
    pkg.add_table(tbl)
    vis = VisualAsset(
        asset_id="vis_001",
        asset_type="chart",
        title="Conductivity Benchmarks",
        url_or_path="/data/artifacts/vis_001.png",
        is_generated=True,
    )
    pkg.add_visual(vis)
    eq = FormulaItem(
        formula_id="eq_001",
        name="Arrhenius Ionic Conductivity",
        latex=r"\sigma = \frac{\sigma_0}{T} \exp\left(-\frac{E_a}{k_B T}\right)",
        explanation="Temperature dependence of ionic transport.",
    )
    pkg.add_formula(eq)
    return pkg


def test_research_checkpoint_persistence_and_reload():
    """Verify complete lossless serialization and deserialization of research package in task checkpoints."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        temp_db = f.name

    try:
        db.init_db(db_path=temp_db)
        pkg = _create_sample_package()

        working_memory = {
            "schema_version": 1,
            "research_stage": "synthesis",
            "research_knowledge_package": pkg.to_dict(),
            "autonomous_discovery": {
                "queries": ["solid-state battery 2024"],
                "selected_domains": ["nature.com"],
            },
        }

        task_id = "task_res_001"
        conv_id = "conv_res_001"

        db.save_task_checkpoint(
            task_id=task_id,
            conversation_id=conv_id,
            session_id="sess_001",
            step_index=3,
            last_url="https://nature.com",
            history=[{"role": "user", "action": "search"}],
            working_memory=working_memory,
            db_path=temp_db,
        )

        # Reload from database (simulating fresh backend restart)
        cp = db.get_task_checkpoint(task_id, db_path=temp_db)
        assert cp is not None
        assert cp["task_id"] == task_id
        assert cp["step_index"] == 3

        wm = cp["working_memory"]
        assert wm["schema_version"] == 1
        assert wm["research_stage"] == "synthesis"

        # Reconstruct canonical package
        reconstructed = ResearchKnowledgePackage.from_dict(wm["research_knowledge_package"])
        assert reconstructed.package_id == "pkg_test_001"
        assert reconstructed.topic.title == "Solid-State Battery Breakthroughs"
        assert len(reconstructed.sources) == 1
        assert reconstructed.sources["src_001"].domain == "nature.com"
        assert len(reconstructed.claims) == 1
        assert reconstructed.claims["clm_001"].claim_type == "statistic"
        assert len(reconstructed.tables) == 1
        assert len(reconstructed.formulas) == 1
        assert len(reconstructed.visuals) == 1

        # Validation integrity scan passes
        assert reconstructed.validate_package_integrity() == []
    finally:
        try:
            Path(temp_db).unlink(missing_ok=True)
        except OSError:
            pass


def test_artifact_crud_operations():
    """Verify artifact registration, retrieval, listing, and deletion."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        temp_db = f.name

    try:
        db.init_db(db_path=temp_db)

        artifact = {
            "artifact_id": "art_docx_001",
            "task_id": "task_res_002",
            "conversation_id": "conv_res_002",
            "filename": "Quantum_Computing_Report.docx",
            "filepath": "/artifacts/task_res_002/Quantum_Computing_Report.docx",
            "file_format": "docx",
            "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "file_size_bytes": 45120,
            "title": "Quantum Computing Report",
            "summary": "Comprehensive 10-page research report with benchmarks and citations.",
            "metadata": {
                "citations_count": 8,
                "tables_count": 2,
                "visuals_count": 3,
            },
        }

        saved = db.save_artifact(artifact, db_path=temp_db)
        assert saved["artifact_id"] == "art_docx_001"

        fetched = db.get_artifact("art_docx_001", db_path=temp_db)
        assert fetched is not None
        assert fetched["filename"] == "Quantum_Computing_Report.docx"
        assert fetched["file_format"] == "docx"
        assert fetched["metadata"]["citations_count"] == 8

        # List by task
        task_arts = db.list_task_artifacts("task_res_002", db_path=temp_db)
        assert len(task_arts) == 1

        # List by conversation
        conv_arts = db.list_conversation_artifacts("conv_res_002", db_path=temp_db)
        assert len(conv_arts) == 1

        # Delete artifact
        deleted = db.delete_artifact("art_docx_001", db_path=temp_db)
        assert deleted is True
        assert db.get_artifact("art_docx_001", db_path=temp_db) is None
    finally:
        try:
            Path(temp_db).unlink(missing_ok=True)
        except OSError:
            pass
