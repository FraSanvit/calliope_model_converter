"""Legacy timeseries CSVs -> v0.7 data tables.

Legacy CSVs are wide: a datetime index column followed by one column per
location. They are kept in that layout. Every CSV of the legacy model is
copied (header cell of the index normalised to `timesteps`, which v0.7 data
tables require); references that need a transformation (sign flip for demand,
1/x for conversion_plus ratios, single-column selection) get a derived CSV.
"""

from __future__ import annotations

import csv
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import pandera.pandas as pa

from converter.diagnostics import Diagnostics

TIMESTEPS = "timesteps"


def read_header(path: Path) -> list[str]:
    with open(path, encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        try:
            return next(reader)
        except StopIteration:
            return []


def looks_like_timeseries(path: Path) -> bool:
    """True if the first column of the second line parses as a datetime."""
    with open(path, encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        try:
            next(reader)
            first = next(reader)
        except StopIteration:
            return False
    if not first:
        return False
    try:
        pd.Timestamp(first[0])
    except (ValueError, TypeError):
        return False
    return True


_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}")


def in_span(timestamp: str, span: tuple[str, str] | None) -> bool:
    """Is an ISO timestamp string within the inclusive (start, end) span of ISO date strings?"""
    if span is None:
        return True
    start, end = span
    return timestamp[: len(start)] >= start and timestamp[: len(end)] <= end


def copy_csv(src: Path, dst: Path, span: tuple[str, str] | None = None, renames: dict[str, str] | None = None) -> None:
    """Stream-copy a CSV, renaming the index header cell to `timesteps` for timeseries.

    If `span` is given, only timeseries rows within it are kept: v0.7 loads whole
    tables before applying `subset`, so unused years would only cost memory.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not looks_like_timeseries(src):
        shutil.copyfile(src, dst)
        return
    with open(src, encoding="utf-8", newline="") as fin, open(dst, "w", encoding="utf-8", newline="") as fout:
        header = next(csv.reader([fin.readline()]))
        header[0] = TIMESTEPS
        header[1:] = [(renames or {}).get(h, h) for h in header[1:]]  # renamed nodes
        csv.writer(fout, lineterminator="\n").writerow(header)
        first = fin.readline()
        if span is None or not _ISO.match(first):
            fout.write(first)
            shutil.copyfileobj(fin, fout, length=16 * 1024 * 1024)
            return
        for line in [first, *fin]:
            if in_span(line.split(",", 1)[0].strip().strip('"'), span):
                fout.write(line)


TIMESERIES_SCHEMA = pa.DataFrameSchema(
    columns={r".*": pa.Column(float, nullable=True, regex=True, coerce=True)},
    index=pa.Index("datetime64[ns]", unique=True, coerce=True),
    strict=False,
)


def validate_csv(path: Path, diag: Diagnostics, source: str | None = None) -> bool:
    """Validate a legacy timeseries CSV (pandera); problems are reported as warnings.

    Returns False if the file cannot be used at all.
    """
    try:
        df = pd.read_csv(path, index_col=0)
    except Exception as err:  # noqa: BLE001 - any parse failure is a user-data problem
        diag.warn("bad_csv", f"Could not read `{path.name}`: {err}", source)
        return False
    if df.empty:
        diag.warn("bad_csv", f"`{path.name}` has no data rows.", source)
        return False
    if df.columns.duplicated().any():
        diag.warn(
            "bad_csv", f"`{path.name}`: duplicated columns {sorted(set(df.columns[df.columns.duplicated()]))}.", source
        )
    try:
        TIMESERIES_SCHEMA.validate(df, lazy=True)
    except pa.errors.SchemaErrors as err:
        cases = err.failure_cases
        summary = "; ".join(
            f"{row.column if isinstance(row.column, str) else 'index'}: {row.check}"
            for row in cases.drop_duplicates(subset=["column", "check"]).head(5).itertuples()
        )
        diag.warn("bad_csv", f"`{path.name}` failed validation ({len(cases)} failure(s)): {summary}", source)
        return False
    n_nan = int(df.isna().sum().sum())
    if n_nan:
        diag.warn("csv_nan", f"`{path.name}`: {n_nan} empty/NaN values (left as-is).", source)
    return True


@dataclass(frozen=True)
class DerivedCsv:
    source: Path
    dest_rel: str  # relative to the output model directory
    transform: str | None  # None | "negate" | "invert"
    column: str | None  # legacy `file=x.csv:column`
    nodes: tuple[str, ...]  # columns to create when `column` is given
    span: tuple[str, str] | None = None  # keep only these timesteps (inclusive ISO dates)
    factor: float | None = None  # multiply after the transform


def write_derived(spec: DerivedCsv, out_dir: Path, diag: Diagnostics, renames: dict[str, str] | None = None) -> bool:
    try:
        df = pd.read_csv(spec.source, index_col=0)
    except Exception as err:  # noqa: BLE001
        diag.warn("bad_csv", f"Could not read `{spec.source.name}`: {err}")
        return False
    if spec.span is not None:
        df = df[[in_span(str(ts), spec.span) for ts in df.index]]
    if spec.column is not None:
        if spec.column not in df.columns:
            diag.warn("missing_column", f"`{spec.source.name}` has no column `{spec.column}`.")
            return False
        df = pd.DataFrame({node: df[spec.column] for node in spec.nodes}, index=df.index)
    else:
        df = df.rename(columns=renames or {})  # renamed nodes
    if spec.transform == "negate":
        df = -df
    elif spec.transform == "invert":
        if (df == 0).any().any():
            diag.warn("zero_ratio", f"`{spec.source.name}` contains zeros; 1/0 replaced by 0 (carrier excluded).")
        df = (1 / df).replace([float("inf"), -float("inf")], 0)
    if spec.factor is not None:
        df = df * spec.factor
    df.index.name = TIMESTEPS
    dest = out_dir / spec.dest_rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(dest, lineterminator="\n")
    return True
