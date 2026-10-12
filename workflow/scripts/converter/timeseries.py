"""Legacy timeseries CSVs -> v0.7 data tables.

Legacy CSVs are wide: a datetime index column followed by one column per
location. They are kept in that layout. Every CSV of the legacy model is
copied (header cell of the index normalised to `timesteps`, which v0.7 data
tables require); references that need a transformation (sign flip for demand,
1/x for conversion_plus ratios, single-column selection) get a derived CSV.

Big files: timesteps are sorted, so the rows of the time span are located by bisection on byte offsets
(`span_slice`); validation, copies and derived CSVs then read only those bytes, in chunks of `CHUNK_ROWS` rows.
Files that are not ISO-dated or not sorted fall back to a full streamed scan.
"""

from __future__ import annotations

import csv
import io
import os
import re
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import pandera.pandas as pa

from converter.diagnostics import Diagnostics

TIMESTEPS = "timesteps"
CHUNK_ROWS = 100_000  # rows per chunk when reading big CSVs
SORT_SAMPLES = 64  # lines sampled to check that a file is sorted before bisecting


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


def _first_field(line: bytes) -> str:
    return line.split(b",", 1)[0].decode("utf-8", "replace").strip().strip('"')


def span_slice(path: Path, span: tuple[str, str] | None) -> tuple[bytes, int, int] | None:
    """(header line, start, end): byte range of the rows within `span`, found by bisection.

    None when there is no span or the file cannot be bisected (no ISO dates, unsorted sample): read it all then.
    """
    if span is None:
        return None
    start, end = span
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        header = f.readline()
        body = f.tell()
        first = f.readline()
        if not first or not _ISO.match(_first_field(first)):
            return None

        def line_from(offset: int) -> tuple[int, str | None]:
            """Start and timestamp of the first line starting at or after `offset`."""
            f.seek(max(offset - 1, body - 1))
            f.readline()  # the rest of the line containing offset - 1
            pos = f.tell()
            line = f.readline()
            return pos, (_first_field(line) if line.strip() else None)

        # Sample the file: bisection is only valid on sorted timestamps.
        samples = [line_from(body + i * (size - body) // SORT_SAMPLES)[1] for i in range(SORT_SAMPLES)]
        stamps = [t for t in samples if t is not None]
        if any(b < a for a, b in zip(stamps, stamps[1:], strict=False)) or not all(_ISO.match(t) for t in stamps):
            return None

        def first_line(pred) -> int:
            lo, hi = body, size
            while lo < hi:
                mid = (lo + hi) // 2
                pos, ts = line_from(mid)
                if ts is None or pred(ts):
                    hi = mid
                else:
                    lo = mid + 1
            return line_from(lo)[0]

        begin = first_line(lambda ts: ts[: len(start)] >= start)
        stop = first_line(lambda ts: ts[: len(end)] > end)
    return header, begin, max(begin, stop)


def read_chunks(path: Path, span: tuple[str, str] | None) -> Iterator[pd.DataFrame]:
    """The rows of `path` within `span`, in chunks (index = first column, as text)."""
    found = span_slice(path, span)
    if found is None:
        for chunk in pd.read_csv(path, index_col=0, chunksize=CHUNK_ROWS, dtype={0: str}):
            if span is not None:  # unreadable timestamps are kept, for validation to report them
                chunk = chunk[[not _ISO.match(str(ts)) or in_span(str(ts), span) for ts in chunk.index]]
            yield chunk
        return
    header, begin, stop = found
    with open(path, "rb") as f:
        f.seek(begin)
        data = f.read(stop - begin)
    if not data.strip():
        return
    yield from pd.read_csv(io.BytesIO(header + data), index_col=0, chunksize=CHUNK_ROWS, dtype={0: str})


def copy_csv(src: Path, dst: Path, span: tuple[str, str] | None = None, renames: dict[str, str] | None = None) -> None:
    """Stream-copy a CSV, renaming the index header cell to `timesteps` for timeseries.

    If `span` is given, only timeseries rows within it are kept: v0.7 loads whole
    tables before applying `subset`, so unused years would only cost memory.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not looks_like_timeseries(src):
        shutil.copyfile(src, dst)
        return
    found = span_slice(src, span)
    if found is not None:  # copy only the bytes of the span
        header, begin, stop = found
        cells = next(csv.reader([header.decode("utf-8")]))
        cells[0] = TIMESTEPS
        cells[1:] = [(renames or {}).get(h, h) for h in cells[1:]]
        with open(src, "rb") as fin, open(dst, "wb") as fout:
            text = io.StringIO()
            csv.writer(text, lineterminator="\n").writerow(cells)
            fout.write(text.getvalue().encode("utf-8"))
            fin.seek(begin)
            remaining = stop - begin
            while remaining > 0:
                block = fin.read(min(remaining, 16 * 1024 * 1024))
                if not block:
                    break
                fout.write(block)
                remaining -= len(block)
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


def validate_csv(path: Path, diag: Diagnostics, source: str | None = None, span: tuple[str, str] | None = None) -> bool:
    """Validate a legacy timeseries CSV (pandera) on the rows the converted model keeps; problems are warnings.

    Only the rows within `span` are checked (the others are trimmed away), chunk by chunk, so memory does not grow with
    the file. Returns False if the file cannot be used at all.
    """
    header = read_header(path)
    if len(header) != len(set(header)):
        duplicated = sorted({h for h in header if header.count(h) > 1})
        diag.warn("bad_csv", f"`{path.name}`: duplicated columns {duplicated}.", source)
    rows, n_nan, previous, unsorted = 0, 0, None, False
    try:
        for chunk in read_chunks(path, span):
            if chunk.empty:
                continue
            TIMESERIES_SCHEMA.validate(chunk, lazy=True)
            index = pd.to_datetime(chunk.index)
            if previous is not None and index[0] == previous:  # duplicates within a chunk: the schema
                diag.warn("bad_csv", f"`{path.name}`: duplicated timestep {index[0]}.", source)
                return False
            if not unsorted and (not index.is_monotonic_increasing or (previous is not None and index[0] < previous)):
                unsorted = True
                diag.warn("csv_unsorted", f"`{path.name}`: timesteps are not sorted (read in full).", source)
            previous = index[-1]
            rows += len(chunk)
            n_nan += int(chunk.isna().sum().sum())
    except pa.errors.SchemaErrors as err:
        cases = err.failure_cases
        summary = "; ".join(
            f"{row.column if isinstance(row.column, str) else 'index'}: {row.check}"
            for row in cases.drop_duplicates(subset=["column", "check"]).head(5).itertuples()
        )
        diag.warn("bad_csv", f"`{path.name}` failed validation ({len(cases)} failure(s)): {summary}", source)
        return False
    except Exception as err:  # noqa: BLE001 - any parse failure is a user-data problem
        diag.warn("bad_csv", f"Could not read `{path.name}`: {err}", source)
        return False
    if not rows:
        where = " within the time span" if span is not None else ""
        diag.warn("bad_csv", f"`{path.name}` has no data rows{where}.", source)
        return False
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
    """Write a transformed copy of the span's rows, chunk by chunk (memory does not grow with the file)."""
    if spec.column is not None and spec.column not in read_header(spec.source)[1:]:
        diag.warn("missing_column", f"`{spec.source.name}` has no column `{spec.column}`.")
        return False
    dest = out_dir / spec.dest_rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    zeros, first = False, True
    try:
        with open(dest, "w", encoding="utf-8", newline="") as out:
            for df in read_chunks(spec.source, spec.span):
                if spec.column is not None:
                    df = pd.DataFrame({node: df[spec.column] for node in spec.nodes}, index=df.index)
                else:
                    df = df.rename(columns=renames or {})  # renamed nodes
                if spec.transform == "negate":
                    df = -df
                elif spec.transform == "invert":
                    zeros = zeros or bool((df == 0).any().any())
                    df = (1 / df).replace([float("inf"), -float("inf")], 0)
                if spec.factor is not None:
                    df = df * spec.factor
                df.index.name = TIMESTEPS
                df.to_csv(out, header=first, lineterminator="\n")
                first = False
    except Exception as err:  # noqa: BLE001
        diag.warn("bad_csv", f"Could not read `{spec.source.name}`: {err}")
        return False
    if zeros:
        diag.warn("zero_ratio", f"`{spec.source.name}` contains zeros; 1/0 replaced by 0 (carrier excluded).")
    return True
