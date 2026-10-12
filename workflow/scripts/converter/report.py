"""Conversion report (markdown + json)."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any

from converter import rules
from converter.diagnostics import Diagnostics
from converter.runpy_inspector import CustomConstraint


def build_report(
    model_name: str,
    entry: str,
    stats: dict[str, Any],
    custom: list[CustomConstraint],
    shadow_prices: list[str],
    diag: Diagnostics,
) -> dict[str, Any]:
    return {
        "model": model_name,
        "entry": entry,
        "stats": stats,
        "custom_constraints": [asdict(c) for c in custom],
        "custom_params": {k: {"v07": v[0], "gallery": v[1]} for k, v in rules.CUSTOM_PARAMS.items()},
        "shadow_prices": shadow_prices,
        "messages": diag.to_list(),
    }


def _md_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    def cell(x: str) -> str:
        return str(x).replace("|", "\\|").replace("\n", " ")

    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(cell(c) for c in row) + " |" for row in rows]
    return lines


def to_markdown(report: dict[str, Any]) -> str:
    lines = [f"# Conversion report: `{report['model']}`", "", f"Legacy entry file: `{report['entry']}`", ""]

    lines += ["## Summary", ""]
    stats = report["stats"]
    lines += _md_table(["item", "count"], [[k, str(v)] for k, v in stats.items() if not isinstance(v, dict)])
    if stats.get("group_types"):
        lines += ["", "Group constraints converted (by type):", ""]
        lines += _md_table(["type", "count"], [[k, str(v)] for k, v in stats["group_types"].items()])

    lines += ["", "## Custom math in run.py", ""]
    lines += [
        "These constraints are added by the legacy `run.py`. Those marked TRANSLATED are part of the converted model "
        "(custom math in `math/`); the others are **not** yet. Their inputs are carried over (renamed, see below). "
        f"Math gallery: {rules.GALLERY_URL}",
        "",
    ]
    rows = []
    for c in report["custom_constraints"]:
        applies = ", ".join(c["applies_to"][:8]) + (" ..." if len(c["applies_to"]) > 8 else "")
        rows.append(
            [
                f"`{c['name']}`",
                f"`{c['trigger']}`",
                ", ".join(f"`{r}`" for r in c["reads"]),
                applies or "not triggered by this model's YAML",
                c["gallery"] or "none",
                c["note"],
            ]
        )
    lines += _md_table(["constraint", "trigger", "reads", "applies to", "gallery", "note"], rows)

    lines += ["", "### Renamed custom parameters", ""]
    lines += _md_table(
        ["legacy", "v0.7", "gallery"],
        [[f"`{k}`", f"`{v['v07']}`", v["gallery"] or "none"] for k, v in report["custom_params"].items()],
    )

    lines += ["", "## Shadow prices (duals)", ""]
    lines += [
        "Configured in `config.solve.shadow_prices`; after solving, read `shadow_price_<constraint>` from the results:",
        "",
    ]
    lines += [
        f"- `{s}`" + (" (energy balance per node, carrier, timestep)" if s == "system_balance" else " (CO2 cap)")
        for s in report["shadow_prices"]
    ]

    messages = report["messages"]
    warnings = [m for m in messages if m["level"] == "warning"]
    lines += ["", f"## Warnings ({len(warnings)})", ""]
    counts = Counter(m["code"] for m in warnings)
    lines += _md_table(["code", "count"], [[k, str(v)] for k, v in counts.most_common()])
    lines += [""]
    for m in warnings:
        src = f" _[{m['source']}]_" if m["source"] else ""
        lines.append(f"- **{m['code']}**{src}: {m['message']}")
    infos = [m for m in messages if m["level"] == "info"]
    if infos:
        lines += ["", f"## Notes ({len(infos)})", ""]
        for m in infos:
            src = f" _[{m['source']}]_" if m["source"] else ""
            lines.append(f"- {m['code']}{src}: {m['message']}")
    return "\n".join(lines) + "\n"


def write_report(report: dict[str, Any], md_path: Path, json_path: Path | None = None) -> None:
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(to_markdown(report), encoding="utf-8")
    if json_path is not None:
        json_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
