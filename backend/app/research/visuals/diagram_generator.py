"""Deterministic Architecture & Workflow Diagram Generator.

Generates syntactically validated Mermaid diagrams and crisp PNG fallback assets
for complex technical architectures (Transformers, Operating Systems, Networks, Batteries).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.patches as patches
import matplotlib.pyplot as plt


def sanitize_mermaid_id(name: str) -> str:
    """Sanitize node identifier to valid alphanumeric Mermaid token."""
    clean = re.sub(r"[^a-zA-Z0-9_]", "_", name.strip())
    return clean or "node"


def generate_mermaid_diagram(
    topic: str,
    diagram_type: str = "architecture",
    custom_nodes: Optional[list[dict[str, str]]] = None,
) -> str:
    """Generate deterministic, syntactically valid Mermaid diagram syntax."""
    t_clean = topic.lower()

    if custom_nodes:
        lines = ["flowchart TD"]
        for n in custom_nodes:
            src = sanitize_mermaid_id(n.get("from", "A"))
            dst = sanitize_mermaid_id(n.get("to", "B"))
            lbl = n.get("label", "").replace('"', "'")
            src_name = n.get("from_name", src).replace('"', "'")
            dst_name = n.get("to_name", dst).replace('"', "'")
            if lbl:
                lines.append(f'    {src}["{src_name}"] -->|"{lbl}"| {dst}["{dst_name}"]')
            else:
                lines.append(f'    {src}["{src_name}"] --> {dst}["{dst_name}"]')
        return "\n".join(lines)

    # Built-in deterministic architectural blueprints
    if "transformer" in t_clean or "attention" in t_clean:
        return (
            "flowchart TD\n"
            '    In["Input Tokens"] --> Emb["Input Embedding + Positional Encoding"]\n'
            '    Emb --> MHA["Multi-Head Self-Attention"]\n'
            '    Emb -.-> Add1["Add & Norm"]\n'
            '    MHA --> Add1\n'
            '    Add1 --> FFN["Position-wise Feed Forward"]\n'
            '    Add1 -.-> Add2["Add & Norm"]\n'
            '    FFN --> Add2\n'
            '    Add2 --> Lin["Linear Layer"]\n'
            '    Lin --> Smax["Softmax Output Probabilities"]'
        )

    if "battery" in t_clean or "solid-state" in t_clean:
        return (
            "flowchart LR\n"
            '    Anode["Solid Lithium Metal Anode"] -->|Li+ Ions Transport| Electrolyte["Solid Electrolyte Separator (Sulfide/Oxide)"]\n'
            '    Electrolyte -->|Li+ Intercalation| Cathode["Composite Cathode (NMC/LFP + SE)"]\n'
            '    Anode -.->|Current Collector| Ext["External Circuit (e- Load)"]\n'
            '    Ext -.-> Cathode'
        )

    if "kernel" in t_clean or "operating system" in t_clean or "os" in t_clean:
        return (
            "flowchart TD\n"
            '    UserApp["User Applications"] --> SysCall["System Call Interface"]\n'
            '    SysCall --> Kernel["OS Kernel Core"]\n'
            '    Kernel --> PM["Process Manager & Scheduler"]\n'
            '    Kernel --> MM["Virtual Memory Manager"]\n'
            '    Kernel --> FS["VFS & File Systems"]\n'
            '    Kernel --> IPC["IPC & Networking"]\n'
            '    PM --> HAL["Hardware Drivers & HAL"]\n'
            '    MM --> HAL\n'
            '    FS --> HAL\n'
            '    IPC --> HAL\n'
            '    HAL --> HW["Physical CPU, RAM & Devices"]'
        )

    if "network" in t_clean or "osi" in t_clean or "protocol" in t_clean:
        return (
            "flowchart TD\n"
            '    L7["Application Layer (HTTP, DNS, SSH)"] --> L4["Transport Layer (TCP, UDP)"]\n'
            '    L4 --> L3["Network Layer (IPv4, IPv6, BGP)"]\n'
            '    L3 --> L2["Data Link Layer (Ethernet, Wi-Fi)"]\n'
            '    L2 --> L1["Physical Layer (Fiber, Copper, Radio)"]'
        )

    # Generic workflow fallback
    return (
        "flowchart TD\n"
        f'    Start["Input: {topic}"] --> Proc["Analysis & Verification"]\n'
        '    Proc --> Synth["Evidence Synthesis"]\n'
        '    Synth --> Deliverable["Multi-Format Artifact Delivery"]'
    )


def render_diagram_to_png(
    topic: str,
    output_path: str,
    title: Optional[str] = None,
    dpi: int = 200,
) -> str:
    """Render a clean block-and-arrow architecture diagram PNG asset using Matplotlib."""
    out_file = Path(output_path).resolve()
    out_file.parent.mkdir(parents=True, exist_ok=True)

    t_clean = topic.lower()
    fig, ax = plt.subplots(figsize=(9, 5.5), dpi=dpi)
    fig.patch.set_facecolor("#FFFFFF")
    ax.set_facecolor("#FFFFFF")
    ax.axis("off")

    diag_title = title or f"{topic.title()} — Architectural Block Diagram"
    ax.text(0.5, 0.95, diag_title, ha="center", va="top", fontsize=13, fontweight="bold", color="#0F172A")

    if "transformer" in t_clean or "attention" in t_clean:
        stages = [
            ("Input Embedding + Positional Encoding", 0.78, "#E0F2FE", "#0284C7"),
            ("Multi-Head Attention", 0.60, "#F0FDF4", "#16A34A"),
            ("Add & Layer Normalization", 0.44, "#FEF3C7", "#D97706"),
            ("Feed Forward Network", 0.28, "#FEE2E2", "#DC2626"),
            ("Linear & Softmax Output", 0.12, "#EDE9FE", "#7C3AED"),
        ]
        for name, y, facecol, edgecol in stages:
            rect = patches.FancyBboxPatch(
                (0.2, y - 0.05), 0.6, 0.09,
                boxstyle="round,pad=0.02,rounding_size=0.03",
                facecolor=facecol, edgecolor=edgecol, linewidth=2,
            )
            ax.add_patch(rect)
            ax.text(0.5, y - 0.005, name, ha="center", va="center", fontsize=10, fontweight="bold", color="#0F172A")

        for i in range(len(stages) - 1):
            y_curr = stages[i][1] - 0.05
            y_next = stages[i + 1][1] + 0.04
            ax.annotate("", xy=(0.5, y_next), xytext=(0.5, y_curr),
                        arrowprops=dict(arrowstyle="->", color="#334155", lw=2, shrinkA=2, shrinkB=2))

    elif "battery" in t_clean or "solid-state" in t_clean:
        blocks = [
            ("Lithium Metal Anode\n(High Energy Density)", 0.12, 0.4, 0.22, 0.35, "#FEF08A", "#CA8A04"),
            ("Solid Electrolyte Separator\n(Sulfide / Oxide LLZO)", 0.39, 0.4, 0.22, 0.35, "#BAE6FD", "#0284C7"),
            ("Composite Cathode\n(NMC / LFP + Solid SE)", 0.66, 0.4, 0.22, 0.35, "#BBF7D0", "#16A34A"),
        ]
        for name, x, y, w, h, facecol, edgecol in blocks:
            rect = patches.FancyBboxPatch(
                (x, y), w, h,
                boxstyle="round,pad=0.02,rounding_size=0.03",
                facecolor=facecol, edgecolor=edgecol, linewidth=2,
            )
            ax.add_patch(rect)
            ax.text(x + w / 2, y + h / 2, name, ha="center", va="center", fontsize=9, fontweight="bold", color="#0F172A")

        # Ion transport arrows
        ax.annotate("Li+ Transport", xy=(0.39, 0.58), xytext=(0.34, 0.58),
                    arrowprops=dict(arrowstyle="->", color="#DC2626", lw=2.5))
        ax.annotate("Li+ Intercalation", xy=(0.66, 0.58), xytext=(0.61, 0.58),
                    arrowprops=dict(arrowstyle="->", color="#DC2626", lw=2.5))

    else:
        # 3-tier system diagram
        tiers = [
            ("Presentation & User Interface Layer", 0.75, "#E2E8F0", "#475569"),
            ("Core Logic, Security & Processing Engine", 0.48, "#DBEAFE", "#2563EB"),
            ("Data Persistence & External Systems Layer", 0.21, "#DCFCE7", "#16A34A"),
        ]
        for name, y, facecol, edgecol in tiers:
            rect = patches.FancyBboxPatch(
                (0.15, y - 0.06), 0.7, 0.12,
                boxstyle="round,pad=0.02,rounding_size=0.03",
                facecolor=facecol, edgecolor=edgecol, linewidth=2,
            )
            ax.add_patch(rect)
            ax.text(0.5, y, name, ha="center", va="center", fontsize=10, fontweight="bold", color="#0F172A")

        for i in range(len(tiers) - 1):
            y_curr = tiers[i][1] - 0.06
            y_next = tiers[i + 1][1] + 0.06
            ax.annotate("", xy=(0.5, y_next), xytext=(0.5, y_curr),
                        arrowprops=dict(arrowstyle="<->", color="#334155", lw=2, shrinkA=3, shrinkB=3))

    fig.tight_layout()
    fig.savefig(str(out_file), bbox_inches="tight", facecolor="white", edgecolor="none")
    plt.close(fig)

    return str(out_file)


def render_attention_diagram_to_png(
    output_path: str,
    title: Optional[str] = None,
    dpi: int = 200,
) -> str:
    """Render Scaled Dot-Product Attention flow pipeline diagram PNG asset."""
    out_file = Path(output_path).resolve()
    out_file.parent.mkdir(parents=True, exist_ok=True)

    fig, ax = plt.subplots(figsize=(8.5, 5), dpi=dpi)
    fig.patch.set_facecolor("#FFFFFF")
    ax.set_facecolor("#FFFFFF")
    ax.axis("off")

    diag_title = title or "Scaled Dot-Product Attention — Computation Pipeline"
    ax.text(0.5, 0.96, diag_title, ha="center", va="top", fontsize=12, fontweight="bold", color="#0F172A")

    nodes = [
        ("Queries (Q) & Keys (K)", 0.18, 0.78, 0.36, 0.08, "#E0F2FE", "#0284C7"),
        ("Values (V)", 0.64, 0.78, 0.24, 0.08, "#FEF3C7", "#D97706"),
        ("MatMul (Q × K^T)", 0.18, 0.60, 0.36, 0.08, "#F0FDF4", "#16A34A"),
        ("Scale (÷ √d_k)", 0.18, 0.44, 0.36, 0.08, "#EDE9FE", "#7C3AED"),
        ("Mask (Optional) & Softmax", 0.18, 0.28, 0.36, 0.08, "#FEE2E2", "#DC2626"),
        ("MatMul with Values (V)", 0.28, 0.12, 0.52, 0.08, "#CCFBF1", "#0D9488"),
    ]
    for name, x, y, w, h, facecol, edgecol in nodes:
        rect = patches.FancyBboxPatch(
            (x, y), w, h,
            boxstyle="round,pad=0.015,rounding_size=0.025",
            facecolor=facecol, edgecolor=edgecol, linewidth=1.8,
        )
        ax.add_patch(rect)
        ax.text(x + w / 2, y + h / 2, name, ha="center", va="center", fontsize=9, fontweight="bold", color="#0F172A")

    # Arrows
    ax.annotate("", xy=(0.36, 0.68), xytext=(0.36, 0.78), arrowprops=dict(arrowstyle="->", color="#334155", lw=1.8))
    ax.annotate("", xy=(0.36, 0.52), xytext=(0.36, 0.60), arrowprops=dict(arrowstyle="->", color="#334155", lw=1.8))
    ax.annotate("", xy=(0.36, 0.36), xytext=(0.36, 0.44), arrowprops=dict(arrowstyle="->", color="#334155", lw=1.8))
    ax.annotate("", xy=(0.46, 0.20), xytext=(0.36, 0.28), arrowprops=dict(arrowstyle="->", color="#334155", lw=1.8))
    ax.annotate("", xy=(0.62, 0.20), xytext=(0.76, 0.78), arrowprops=dict(arrowstyle="->", color="#D97706", lw=1.8, connectionstyle="arc3,rad=-0.2"))

    fig.tight_layout()
    fig.savefig(str(out_file), bbox_inches="tight", facecolor="white", edgecolor="none")
    plt.close(fig)
    return str(out_file)


