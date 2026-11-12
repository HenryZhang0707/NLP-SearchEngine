import json
import pytest
from contracts import analyzer
from offline.build import build
from offline.disk_index import DiskIndex
from online.scorers import bm25
from evaluation.sharded_bm25 import shard_topk, merge_topk


def test_global_shards_match_monolithic_with_unequal_df_and_lengths(tmp_path):
    corpus = [dict(doc_id=str(i), title='', text=text) for i, text in enumerate([
        '苹果 苹果 香蕉', '香蕉', '苹果', '汽车 汽车 汽车 汽车', '香蕉 汽车', '苹果 香蕉 汽车'])]
    def index(name, rows):
        source = tmp_path / (name + '.jsonl')
        source.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows))
        build(source, tmp_path / name, name)
        return DiskIndex(tmp_path / name)
    whole = index('whole', corpus)
    shards = [index('left', corpus[:2]), index('right', corpus[2:])]
    for query in ['苹果', '香蕉 汽车', '苹果 苹果 汽车', '不存在']:
        terms = [t.term for t in analyzer.analyze(query)]
        dfs = {t: sum(s.df(t) for s in shards) for t in terms}
        avgdl = sum(s.n_docs * s.avgdl for s in shards) / whole.n_docs
        for k in (1, 3, 10):
            left = shard_topk(shards[0], terms, n_docs=whole.n_docs, avgdl=avgdl,
                             dfs=dfs, offset=0, k=k)
            right = shard_topk(shards[1], terms, n_docs=whole.n_docs, avgdl=avgdl,
                              dfs=dfs, offset=2, k=k)
            got = merge_topk([left, right], k)
            expected = sorted(bm25(whole, terms).items(), key=lambda x: (-x[1], x[0]))[:k]
            assert [d for d, _ in got] == [d for d, _ in expected]
            assert [s for _, s in got] == pytest.approx([s for _, s in expected], abs=1e-12)
    with pytest.raises(KeyError):
        shard_topk(shards[0], ['missing'], n_docs=6, avgdl=1, dfs={}, offset=0, k=1)


def test_resume_rejects_corrupt_completed_shard(tmp_path):
    from evaluation.build_full_sharded_bm25 import verified
    corpus = tmp_path / 'source.jsonl'
    corpus.write_text(json.dumps(dict(doc_id='a', title='', text='苹果')) + '\n')
    output = tmp_path / 'index'
    build(corpus, output, 'dureader-full-shard-000')
    assert verified(output, 'dureader-full-shard-000').n_docs == 1
    with (output / 'postings.jsonl').open('a') as f:
        f.write('\n')
    with pytest.raises(ValueError, match='corrupt'):
        verified(output, 'dureader-full-shard-000')
