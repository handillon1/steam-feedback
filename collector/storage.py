"""Parquet writing shared by the collectors."""
import os
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq


def write_parquet(rows: list[dict], schema: pa.Schema, path: Path) -> None:
    """Write rows with an explicit schema, atomically.

    The explicit schema stops an all-None column being written as type "null", which clashes with other files.
    The file goes to a dot-prefixed .tmp first and is moved into place, so a crash never leaves a half file that
    later runs would skip, and a leftover .tmp is ignored by pd.read_parquet(dir).
    """
    write_table(pa.Table.from_pylist(rows, schema=schema), path)


def write_table(table: pa.Table, path: Path) -> None:
    """write_parquet for a table that's already built (e.g. from numpy), with the same atomic .tmp-then-move."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    pq.write_table(table, tmp)
    os.replace(tmp, path)
