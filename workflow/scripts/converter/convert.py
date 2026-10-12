"""Top-level conversion API and command line interface.

Usage:
    python -m converter.convert <legacy_model_dir> <output_model_dir> --report report.md [--log convert.log]
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

from converter.builder import ModelBuilder
from converter.diagnostics import ConversionError, Diagnostics
from converter.legacy import find_entry_files, load_legacy_model
from converter.report import build_report, write_report
from converter.runpy_inspector import find_applications, inspect_run_py, storage_ev_name_filter
from converter.runscripts import expand_scenario, find_run_scripts, v07_run_script
from converter.writer import write_model

DEFAULT_EXCLUDE = ["*_.yaml"]


@dataclass
class ConversionResult:
    entry: Path
    output_entry: Path
    report: dict
    diagnostics: Diagnostics


def _check_run_scripts(model_root: Path, legacy, diag: Diagnostics) -> list:
    """Legacy run scripts: the overrides they select by name must exist (legacy scenario names are expanded)."""
    scripts = []
    for script in find_run_scripts(model_root, diag):
        items = expand_scenario(script.overrides, legacy.scenarios)
        unknown = [i for i in items if i not in legacy.overrides]
        if unknown:
            diag.warn(
                "missing_override",
                f"Run script `{script.path.name}` selects {unknown}, which are not overrides of this model: "
                "the v0.7 run script will stop with an error until they are fixed.",
            )
        diag.info(
            "run_script",
            f"`{script.path.name}` -> v0.7 run script `{script.path.name}` next to the converted model folder: same settings, "
            f"overrides selected by name ({len(items)}) and merged as in v0.6 (without `run.py` custom math).",
        )
        scripts.append(script)
    return scripts


def resolve_entry(model_root: Path, entry: str | None, exclude: list[str] | None) -> Path:
    model_dir = model_root / "model"
    if not model_dir.is_dir():
        raise ConversionError(f"{model_root} has no `model/` folder; is it a legacy model directory?")
    if entry:
        path = model_dir / entry
        if not path.exists():
            raise ConversionError(f"Configured entry file {path} does not exist.")
        return path
    entries = find_entry_files(model_dir, exclude if exclude is not None else DEFAULT_EXCLUDE)
    if not entries:
        raise ConversionError(f"No YAML file with a top-level `model:` key found in {model_dir}.")
    if len(entries) > 1:
        names = ", ".join(str(e.relative_to(model_dir)) for e in entries)
        raise ConversionError(f"Several model entry files found ({names}); set `entry` for this model in the config.")
    return entries[0]


def convert_model(
    model_root: Path,
    out_dir: Path,
    *,
    entry: str | None = None,
    exclude: list[str] | None = None,
    report_md: Path | None = None,
    report_json: Path | None = None,
    copy_csvs: bool = True,
    trim_timeseries: bool = True,
    contract_group_families: bool = True,
) -> ConversionResult:
    """Convert the legacy model in `model_root` (containing `model/` and `run.py`) into `out_dir`."""
    diag = Diagnostics()
    model_root = Path(model_root)
    entry_path = resolve_entry(model_root, entry, exclude)
    legacy = load_legacy_model(entry_path, model_root / "model", diag)
    run_scripts = _check_run_scripts(model_root, legacy, diag)
    custom = inspect_run_py(model_root / "run.py", diag)
    builder = ModelBuilder(
        legacy,
        diag,
        trim_timeseries=trim_timeseries,
        contract_group_families=contract_group_families,
        runpy_constraints={c.name for c in custom},
        storage_ev_filter=storage_ev_name_filter(model_root / "run.py"),
    )
    converted = builder.build()
    find_applications(custom, legacy, builder.ctx)
    write_model(converted, Path(out_dir), diag, copy_csvs=copy_csvs)
    # As in the legacy folder, run scripts sit next to the model folder (`<model folder>/../run-*.py`).
    model_from_script = f"{Path(out_dir).name}/{converted.entry_rel}"
    for script in run_scripts:
        text = v07_run_script(script, model_from_script, builder.shadow_prices)
        (Path(out_dir).parent / script.path.name).write_text(text, encoding="utf-8")
    report = build_report(model_root.name, converted.entry_rel, converted.stats, custom, builder.shadow_prices, diag)
    if report_md is not None:
        write_report(report, Path(report_md), Path(report_json) if report_json else None)
    return ConversionResult(entry_path, Path(out_dir) / converted.entry_rel, report, diag)


def setup_logging(log_file: Path | None) -> None:
    logger = logging.getLogger("converter")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    handler: logging.Handler = (
        logging.FileHandler(log_file, mode="w", encoding="utf-8") if log_file else logging.StreamHandler()
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger.addHandler(handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("legacy_model", type=Path, help="Legacy model folder (contains model/ and run.py)")
    parser.add_argument("output", type=Path, help="Output folder for the v0.7 model")
    parser.add_argument("--entry", default=None, help="Entry YAML relative to model/ (auto-detected if omitted)")
    parser.add_argument("--report", type=Path, default=None, help="Markdown report path")
    parser.add_argument("--report-json", type=Path, default=None, help="JSON report path")
    parser.add_argument("--log", type=Path, default=None, help="Log file path")
    parser.add_argument(
        "--keep-all-timesteps", action="store_true", help="Do not trim timeseries to the legacy `subset_time`"
    )
    parser.add_argument(
        "--no-contract-groups",
        action="store_true",
        help="One constraint per legacy group (do not merge per-region group families)",
    )
    args = parser.parse_args(argv)
    if args.log:
        args.log.parent.mkdir(parents=True, exist_ok=True)
    setup_logging(args.log)
    try:
        result = convert_model(
            args.legacy_model,
            args.output,
            entry=args.entry,
            report_md=args.report,
            report_json=args.report_json,
            trim_timeseries=not args.keep_all_timesteps,
            contract_group_families=not args.no_contract_groups,
        )
    except ConversionError as err:
        logging.getLogger("converter").error(str(err))
        print(f"ERROR: {err}", file=sys.stderr)
        return 1
    print(f"Converted {result.entry} -> {result.output_entry} ({len(result.diagnostics.warnings)} warnings)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
