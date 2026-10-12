"""Build docs/figures/dag.png: the workflow rules with their inputs and outputs (Snakemake file graph).

The rules are independent (no edges), so Graphviz would order them arbitrarily: invisible edges on one rank put
them left to right as `convert`, `compare`, `fixed_design`. Needs the ERS_test_example inputs (incl. the comparison
case week_1h):

    pixi run dag
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "figures" / "dag.png"
TARGETS = [
    "results/ERS_test_example/conversion_report.md",
    "results/ERS_test_example/comparison/week_1h/comparison.md",
    "results/ERS_test_example/comparison/week_1h/fixed_design.md",
]
ORDER = ["convert", "compare", "fixed_design"]


def main() -> int:
    """Render the file graph with the rules in a fixed order."""
    dot = subprocess.run(
        ["snakemake", "-s", "workflow/Snakefile", "--filegraph", *TARGETS],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    dot = dot[dot.index("digraph") :]  # drop any log lines before the graph
    ids = {}
    for node_id, body in re.findall(r"^(\d+) \[(.*?)\]\s*$", dot, flags=re.MULTILINE | re.DOTALL):
        name = re.search(r'<font point-size="18">([^<]+)</font>', body)
        if name:
            ids[name.group(1)] = node_id
    chain = " -> ".join(ids[rule] for rule in ORDER if rule in ids)
    if chain.count("->") >= 1:
        dot = dot.rstrip().removesuffix("}") + f"{{ rank=same; {chain} [style=invis]; }}\n}}\n"
    subprocess.run(["dot", "-Tpng", "-Gdpi=130", "-o", str(OUT)], input=dot, text=True, check=True, cwd=ROOT)
    print(f"written {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
