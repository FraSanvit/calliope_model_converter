"""Big-file handling of timeseries CSVs: span found by bisection, chunked reads, same results as a full read."""

import numpy as np
import pandas as pd
import pytest
from converter import timeseries
from converter.diagnostics import Diagnostics
from converter.timeseries import DerivedCsv, copy_csv, read_chunks, span_slice, validate_csv, write_derived

SPAN = ("2018-01-01", "2018-12-31")


def _write(path, index, nodes=("A", "B")):
    data = {n: np.arange(len(index), dtype=float) + i for i, n in enumerate(nodes)}
    pd.DataFrame(data, index=pd.Index(index, name="datetime")).to_csv(path, date_format="%Y-%m-%d %H:%M")
    return path


@pytest.fixture
def three_years(tmp_path):
    return _write(tmp_path / "ts.csv", pd.date_range("2017-01-01", "2019-12-31 23:00", freq="h"))


def _expected_lines(path, span):
    lines = path.read_text().splitlines()
    return [line for line in lines[1:] if timeseries.in_span(line.split(",")[0], span)]


def test_span_found_by_bisection(three_years):
    header, begin, stop = span_slice(three_years, SPAN)
    assert header.startswith(b"datetime,")
    chunk = three_years.read_bytes()[begin:stop].decode().splitlines()
    assert chunk == _expected_lines(three_years, SPAN)
    assert chunk[0].startswith("2018-01-01 00:00")
    assert chunk[-1].startswith("2018-12-31 23:00")


@pytest.mark.parametrize(
    "span", [("2016-01-01", "2016-12-31"), ("2020-01-01", "2020-12-31"), ("2019-12-31", "2030-01-01")]
)
def test_span_at_the_edges(three_years, span):
    rows = sum(len(c) for c in read_chunks(three_years, span))
    assert rows == len(_expected_lines(three_years, span))


def test_chunked_read_and_copy_match_a_full_filter(three_years, tmp_path, monkeypatch):
    monkeypatch.setattr(timeseries, "CHUNK_ROWS", 1000)  # several chunks
    chunks = list(read_chunks(three_years, SPAN))
    assert len(chunks) > 1
    assert sum(len(c) for c in chunks) == 8760
    copy_csv(three_years, tmp_path / "copy.csv", SPAN)
    copied = (tmp_path / "copy.csv").read_text().splitlines()
    assert copied[0] == "timesteps,A,B"
    assert copied[1:] == _expected_lines(three_years, SPAN)


def test_unsorted_file_falls_back_to_a_full_scan(tmp_path):
    index = list(pd.date_range("2018-01-01", periods=48, freq="h"))
    index = index[24:] + index[:24]  # the second day first
    path = _write(tmp_path / "unsorted.csv", index)
    assert span_slice(path, ("2018-01-01", "2018-01-01")) is None
    rows = sum(len(c) for c in read_chunks(path, ("2018-01-01", "2018-01-01")))
    assert rows == 24
    diag = Diagnostics()
    assert validate_csv(path, diag, span=("2018-01-01", "2018-01-02"))  # usable, but warned
    assert "csv_unsorted" in {m.code for m in diag.messages}


def test_non_iso_dates_are_read_in_full(tmp_path):
    path = tmp_path / "us.csv"
    path.write_text("datetime,A\n01/01/2018 00:00,1\n01/01/2018 01:00,2\n")
    assert span_slice(path, SPAN) is None


def test_validation_covers_the_kept_rows_only(three_years, monkeypatch):
    monkeypatch.setattr(timeseries, "CHUNK_ROWS", 1000)
    text = three_years.read_text()
    three_years.write_text(
        text.replace("2017-06-01 05:00,", "2017-06-01 05:00,x").replace("\n2019-02-01 05:00,", "\n2019-02-01 05:00,y")
    )
    assert validate_csv(three_years, Diagnostics(), span=SPAN)  # the bad rows are trimmed away
    three_years.write_text(text.replace("2018-06-01 05:00,", "2018-06-01 05:00,x"))
    diag = Diagnostics()
    assert not validate_csv(three_years, diag, span=SPAN)
    assert any(m.code == "bad_csv" for m in diag.messages)


def test_duplicate_timestep_across_chunks_is_caught(tmp_path, monkeypatch):
    monkeypatch.setattr(timeseries, "CHUNK_ROWS", 10)
    index = list(pd.date_range("2018-01-01", periods=30, freq="h"))
    index.insert(10, index[9])  # duplicate at a chunk boundary
    path = _write(tmp_path / "dup.csv", index)
    assert not validate_csv(path, Diagnostics(), span=SPAN)


def test_derived_csv_written_in_chunks(three_years, tmp_path, monkeypatch):
    monkeypatch.setattr(timeseries, "CHUNK_ROWS", 1000)
    spec = DerivedCsv(three_years, "neg.csv", "negate", None, ("A", "B"), SPAN, 2.0)
    assert write_derived(spec, tmp_path, Diagnostics())
    out = pd.read_csv(tmp_path / "neg.csv", index_col=0)
    full = pd.read_csv(three_years, index_col=0)
    full = full[[timeseries.in_span(t, SPAN) for t in full.index]]
    assert out.index.name == "timesteps"
    assert len(out) == 8760
    assert np.allclose(out.to_numpy(), -2.0 * full.to_numpy())
