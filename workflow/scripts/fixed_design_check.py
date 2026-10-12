"""Snakemake entry point of the optional `fixed_design` rule."""

import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

sys.path.insert(0, str(Path(__file__).parent))

from converter.fixed_design import check  # noqa: E402

if TYPE_CHECKING:
    snakemake: Any


def main() -> None:
    """Write `fix_v06_design.yaml` and the verdict report next to the report path given by snakemake."""
    log_file = Path(snakemake.log[0])
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=log_file, level=logging.INFO)
    report = Path(snakemake.output.report)
    verdict = check(Path(snakemake.input.v07).parent, report.parent)
    logging.getLogger("fixed_design").info("verdict: %s", verdict.splitlines()[0])


if __name__ == "__main__":
    main()
