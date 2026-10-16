import json

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from collector import embed
from collector.embed import check_meta, clean_for_embedding, done_ids, embed_new

META = {"model": embed.MODEL, "max_seq_length": 256, "normalize": True}


def fake_encoder():
    """Random unit vectors instead of the 90 MB model; records every text it's given."""
    seen = []
    rng = np.random.default_rng(0)

    def encode(texts):
        seen.extend(texts)
        v = rng.normal(size=(len(texts), embed.DIM)).astype(np.float32)
        return v / np.linalg.norm(v, axis=1, keepdims=True)
    return encode, seen


def rows(ids):
    return [(i, f"review {i}") for i in ids]


# clean_for_embedding

def test_tags_are_removed_and_their_text_kept():
    assert clean_for_embedding("[h1]Verdict[/h1]\n[b]Great[/b] game, [i]really[/i]") == "Verdict\nGreat game, really"


def test_url_keeps_its_link_text():
    assert clean_for_embedding("see [url=https://example.com/x?a=1]this guide[/url] first") == "see this guide first"


def test_lists_spoilers_and_quotes_keep_their_text():
    text = "[list][*]crashes[*]no saves[/list][spoiler]the ending[/spoiler][quote=dev]soon[/quote]"
    assert clean_for_embedding(text) == "crashes no saves the ending soon"


def test_brackets_that_arent_tags_are_kept():
    assert clean_for_embedding("[10/10] would play again [x] fun") == "[10/10] would play again [x] fun"


# resuming, chunking, schema

def test_only_new_ids_are_encoded(tmp_path):
    encode, seen = fake_encoder()
    assert embed_new(iter(rows(range(1, 6))), {1, 2}, encode, tmp_path, META) == 3
    assert seen == ["review 3", "review 4", "review 5"]
    assert done_ids(tmp_path) == {3, 4, 5}


def test_rerun_continues_where_it_stopped_and_then_does_nothing(tmp_path):
    encode, seen = fake_encoder()
    embed_new(iter(rows(range(1, 4))), done_ids(tmp_path), encode, tmp_path, META)
    assert embed_new(iter(rows(range(1, 6))), done_ids(tmp_path), encode, tmp_path, META) == 2
    assert embed_new(iter(rows(range(1, 6))), done_ids(tmp_path), encode, tmp_path, META) == 0
    assert done_ids(tmp_path) == {1, 2, 3, 4, 5}
    assert len(seen) == 5


def test_25000_rows_in_chunks_of_10000_make_3_files(tmp_path):
    encode, _ = fake_encoder()
    assert embed_new(iter(rows(range(1, 25_001))), set(), encode, tmp_path, META, chunk=10_000) == 25_000
    assert sorted(p.name for p in tmp_path.glob("part-*.parquet")) == \
        ["part-1.parquet", "part-10001.parquet", "part-20001.parquet"]


def test_written_file_has_384_float32_values_per_row(tmp_path):
    encode, _ = fake_encoder()
    embed_new(iter(rows([7, 8])), set(), encode, tmp_path, META)
    table = pq.read_table(tmp_path / "part-7.parquet")
    assert table.schema.field("recommendationid").type == pa.int64()
    assert table.schema.field("embedding").type == pa.list_(pa.float32(), 384)
    vecs = np.array(table.column("embedding").to_pylist(), dtype=np.float32)
    assert vecs.shape == (2, 384)
    assert np.allclose(np.linalg.norm(vecs, axis=1), 1, atol=1e-5)


def test_cleaned_text_is_what_gets_encoded(tmp_path):
    encode, seen = fake_encoder()
    embed_new(iter([(1, "[b]bad[/b] port")]), set(), encode, tmp_path, META)
    assert seen == ["bad port"]


def test_wrong_vector_width_raises_before_writing(tmp_path):
    with pytest.raises(ValueError):
        embed_new(iter(rows([1])), set(), lambda t: np.zeros((len(t), 768), np.float32), tmp_path, META)
    assert list(tmp_path.glob("part-*.parquet")) == []


# metadata guard

def test_meta_is_written_when_missing(tmp_path):
    check_meta(tmp_path, META)
    assert json.loads((tmp_path / "_meta.json").read_text(encoding="utf-8")) == META


def test_different_model_in_meta_raises_and_nothing_is_written(tmp_path):
    (tmp_path / "_meta.json").write_text(json.dumps({**META, "model": "some/other-model"}), encoding="utf-8")
    encode, seen = fake_encoder()
    with pytest.raises(ValueError, match="other-model"):
        embed_new(iter(rows([1, 2])), set(), encode, tmp_path, META)
    assert seen == []
    assert list(tmp_path.glob("part-*.parquet")) == []
