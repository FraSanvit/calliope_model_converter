"""Snakemake entry point of the `convert` rule."""

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

sys.path.insert(0, str(Path(__file__).parent))

from converter.convert import convert_model, setup_logging  # noqa: E402

if TYPE_CHECKING:
    snakemake: Any


def main() -> None:
    """Run the conversion with the inputs, outputs and params given by snakemake."""
    log_file = Path(snakemake.log[0])
    log_file.parent.mkdir(parents=True, exist_ok=True)
    setup_logging(log_file)
    convert_model(
        Path(snakemake.input.legacy),
        Path(snakemake.output.model),
        entry=snakemake.params.entry,
        exclude=snakemake.params.exclude_entries,
        report_md=Path(snakemake.output.report),
        report_json=Path(snakemake.output.report_json),
        copy_csvs=snakemake.params.copy_csvs,
        trim_timeseries=snakemake.params.trim_timeseries,
        contract_group_families=snakemake.params.contract_group_families,
    )


if __name__ == "__main__":
    main()
