"""Snakemake entry point of the `compare` rule."""

import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

sys.path.insert(0, str(Path(__file__).parent))

from converter.compare import compare  # noqa: E402

if TYPE_CHECKING:
    snakemake: Any


def main() -> None:
    """Compare the runs given by snakemake; the CSV tables go next to the report."""
    log_file = Path(snakemake.log[0])
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=log_file, level=logging.INFO)
    report = Path(snakemake.output.report)
    summary = compare(Path(snakemake.input.v07).parent, report.parent)
    logging.getLogger("compare").info("summary: %s", summary)


if __name__ == "__main__":
    main()
