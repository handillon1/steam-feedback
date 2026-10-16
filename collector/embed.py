"""Embed every non-short review with all-MiniLM-L6-v2 (D20). Laptop only, not in the workflows.

    python -m collector.embed

Resumes by ID, not by chunk number: reads the recommendationids already in data/embeddings/, streams the
non-short reviews from Postgres with a server-side cursor, and embeds only the rest. An interrupted run continues,
a rerun after new reviews embeds only those, and a rerun with nothing new does nothing.

Output: data/embeddings/part-<first recommendationid>.parquet (recommendationid int64, embedding 384 x float32)
and data/embeddings/_meta.json, which stops vectors from a different model or settings being mixed in.
The database keeps the original text; only the model sees clean_for_embedding's version.
sentence_transformers (torch) is imported in main() only, so the tests and CI never need it.
"""
import json
import logging
import os
import re
import time
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path

import numpy as np
import psycopg
import pyarrow as pa
import pyarrow.parquet as pq
from dotenv import load_dotenv

from collector.reviews import DATA
from collector.storage import write_table

log = logging.getLogger(__name__)

MODEL = "sentence-transformers/all-MiniLM-L6-v2"
DIM = 384
EMB_DIR = DATA / "embeddings"
CHUNK = 10_000   # reviews per part file, and per fetch from the server-side cursor
SCHEMA = pa.schema([("recommendationid", pa.int64()), ("embedding", pa.list_(pa.float32(), DIM))])

# Steam's BBCode tags, by name, so bracketed text that isn't a tag ("[10/10]", "[x] fun") is kept.
_BBCODE = re.compile(r"\[/?(?:h1|h2|h3|b|u|i|strike|spoiler|noparse|hr|url|list|olist|\*|quote|code|table|tr|th|td)"
                     r"(?:=[^\]]*)?\]", re.I)


def clean_for_embedding(text: str) -> str:
    """The review without BBCode tags: the text inside them is kept ([url=...]text[/url] -> text), spaces tidied."""
    text = _BBCODE.sub(" ", text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r" *\n *", "\n", text).strip()


def done_ids(emb_dir: Path) -> set[int]:
    """recommendationids already embedded. Reads only that column; .tmp leftovers don't match part-*."""
    ids = set()
    for p in emb_dir.glob("part-*.parquet"):
        ids.update(pq.read_table(p, columns=["recommendationid"]).column(0).to_pylist())
    return ids


def check_meta(emb_dir: Path, meta: dict) -> None:
    """Write _meta.json if it's missing; raise if it describes a different model or settings.

    Vectors from different models live in different spaces: comparing them gives meaningless results, no error.
    """
    path = emb_dir / "_meta.json"
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing != meta:
            raise ValueError(f"{path} says {existing}, this run is {meta}: refusing to mix vectors")
        return
    emb_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta, indent=1), encoding="utf-8")


def _write_part(ids: list[int], vecs: np.ndarray, emb_dir: Path) -> None:
    if vecs.shape != (len(ids), DIM):
        raise ValueError(f"encoder returned shape {vecs.shape}, expected ({len(ids)}, {DIM})")
    flat = pa.array(np.ascontiguousarray(vecs, dtype=np.float32).ravel(), pa.float32())
    table = pa.Table.from_arrays([pa.array(ids, pa.int64()), pa.FixedSizeListArray.from_arrays(flat, DIM)],
                                 schema=SCHEMA)
    write_table(table, emb_dir / f"part-{ids[0]}.parquet")


def embed_new(rows: Iterable[tuple[int, str]], done: set[int], encode: Callable[[list[str]], np.ndarray],
              emb_dir: Path, meta: dict, chunk: int = CHUNK) -> int:
    """Embed the (recommendationid, review) rows not in done, CHUNK per part file. Returns how many were embedded.

    encode is model.encode in main() and a fake in tests. The metadata check comes first, so a mismatch writes
    nothing. Part files are named by their first ID; it can't collide with an existing part, since every
    existing part's first ID is already done.
    """
    check_meta(emb_dir, meta)
    ids, texts, total, started = [], [], 0, time.monotonic()
    scanned = skipped = 0

    def flush():
        nonlocal total
        _write_part(ids, encode(texts), emb_dir)
        total += len(ids)
        log.info("%d embedded (%.0f/s), last part-%d.parquet", total, total / (time.monotonic() - started), ids[0])
        ids.clear()
        texts.clear()

    for rid, review in rows:
        scanned += 1
        if scanned % 100_000 == 0:
            log.info("scanned %d reviews: %d already embedded, %d new so far", scanned, skipped, total + len(ids))
        if rid in done:
            skipped += 1
            continue
        ids.append(rid)
        texts.append(clean_for_embedding(review))
        if len(ids) == chunk:
            flush()
    if ids:
        flush()
    log.info("scanned %d reviews: %d already embedded, %d new", scanned, skipped, total)
    return total


def stream_reviews(conn: psycopg.Connection) -> Iterator[tuple[int, str]]:
    """Every non-short review, CHUNK at a time from a server-side (named) cursor, never all ~700k in memory."""
    with conn.cursor(name="embed") as cur:
        cur.execute("SELECT recommendationid, review FROM reviews WHERE NOT is_short ORDER BY recommendationid")
        while batch := cur.fetchmany(CHUNK):
            yield from batch


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)   # model loading logs every Hugging Face request
    from sentence_transformers import SentenceTransformer   # here, not at the top: keeps torch out of CI

    model = SentenceTransformer(MODEL, device="cpu")
    meta = {"model": MODEL, "max_seq_length": model.max_seq_length, "normalize": True}
    done = done_ids(EMB_DIR)
    log.info("%d reviews already embedded; streaming non-short reviews to find new ones "
             "(a few minutes with no output when nothing is new)", len(done))

    def encode(texts):
        return model.encode(texts, batch_size=64, normalize_embeddings=True, convert_to_numpy=True,
                            show_progress_bar=False)

    load_dotenv()
    with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=10) as conn:
        n = embed_new(stream_reviews(conn), done, encode, EMB_DIR, meta)
    log.info("done: %d new reviews embedded", n)


if __name__ == "__main__":
    main()
