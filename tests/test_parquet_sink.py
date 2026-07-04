"""ParquetSink: flush correctness, schema, row groups."""

from __future__ import annotations

import pyarrow.parquet as pq

from drobit_rig.scale_reader import SCHEMA, ParquetSink


def test_schema_and_values_roundtrip(tmp_path):
    path = tmp_path / "scale.parquet"
    sink = ParquetSink(path)
    sink.append(7, 0, 12_500, 1_000_000_000, -42)
    sink.append(7, 1, 25_000, 1_012_500_000, 2**31 - 1)
    sink.close()

    table = pq.read_table(path)
    assert table.schema.equals(SCHEMA)
    assert table.num_rows == 2
    assert table.column("session").to_pylist() == [7, 7]
    assert table.column("seq").to_pylist() == [0, 1]
    assert table.column("esp_us").to_pylist() == [12_500, 25_000]
    assert table.column("rpi_mono_ns").to_pylist() == [1_000_000_000, 1_012_500_000]
    assert table.column("raw").to_pylist() == [-42, 2**31 - 1]


def test_flush_clears_buffer_and_counts(tmp_path):
    sink = ParquetSink(tmp_path / "scale.parquet")
    for i in range(10):
        sink.append(1, i, i, i, i)
    assert len(sink) == 10
    assert sink.flush() == 10
    assert len(sink) == 0
    assert sink.rows_written == 10
    assert sink.flush() == 0  # empty flush is a no-op
    sink.close()


def test_one_row_group_per_flush(tmp_path):
    path = tmp_path / "scale.parquet"
    sink = ParquetSink(path)
    for group in range(3):
        for i in range(5):
            sink.append(1, group * 5 + i, 0, 0, 0)
        sink.flush()
    sink.close()

    pf = pq.ParquetFile(path)
    assert pf.metadata.num_row_groups == 3
    assert pf.metadata.num_rows == 15
    table = pf.read()
    assert table.column("seq").to_pylist() == list(range(15))


def test_close_flushes_remaining_rows(tmp_path):
    path = tmp_path / "scale.parquet"
    sink = ParquetSink(path)
    sink.append(1, 0, 0, 0, 0)
    sink.close()  # no explicit flush
    assert pq.read_table(path).num_rows == 1


def test_large_u64_values(tmp_path):
    path = tmp_path / "scale.parquet"
    sink = ParquetSink(path)
    big = 2**63 + 5  # u64 territory beyond i64
    sink.append(2**32 - 1, 2**32 - 1, big, big, -(2**31))
    sink.close()
    row = pq.read_table(path).to_pylist()[0]
    assert row["esp_us"] == big
    assert row["rpi_mono_ns"] == big
    assert row["session"] == 2**32 - 1
    assert row["raw"] == -(2**31)
