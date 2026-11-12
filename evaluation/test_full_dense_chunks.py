from dataclasses import dataclass
import json
import pytest
from evaluation.encode_full_dense_chunks import encode_shard


@dataclass
class Spec:
    dimension: int = 2


class Encoder:
    spec = Spec()
    def __init__(self):
        self.calls = []
    def encode_documents(self, texts):
        self.calls.append(texts)
        return [[1., 0.]] * len(texts)


def test_chunk_resume_binds_source_ids_and_vector_bytes(tmp_path):
    source = tmp_path / 'corpus'
    source.write_text(''.join(json.dumps(dict(doc_id=str(i), title='标题', text='正文')) + '\n' for i in range(5)))
    encoder = Encoder()
    out = tmp_path / 'chunks'
    result = encode_shard(source, out, encoder, 2)
    assert result['documents'] == 5
    assert len(encoder.calls) == 3
    assert encoder.calls[0] == ['标题\n正文'] * 2
    encoder.calls.clear()
    encode_shard(source, out, encoder, 2)
    assert not encoder.calls
    (out / 'chunk-00000.npy').write_bytes(b'broken')
    with pytest.raises(ValueError, match='corruption'):
        encode_shard(source, out, encoder, 2)


def test_source_change_is_rejected_before_reusing_chunk(tmp_path):
    source = tmp_path / 'corpus'
    source.write_text(json.dumps(dict(doc_id='a', title='', text='one')) + '\n')
    out = tmp_path / 'out'
    encode_shard(source, out, Encoder())
    source.write_text(json.dumps(dict(doc_id='b', title='', text='two')) + '\n')
    with pytest.raises(ValueError, match='identity'):
        encode_shard(source, out, Encoder())


def test_stream_records_use_global_ids_and_reject_truncated_summary(tmp_path):
    from evaluation.stream_dense_chunks import iter_shard_records
    source = tmp_path / 'corpus'
    source.write_text(''.join(json.dumps(dict(doc_id=str(i), title='', text='text')) + '\n' for i in range(5)))
    out = tmp_path / 'encoded'
    encode_shard(source, out, Encoder(), chunk_size=2)
    records = list(iter_shard_records(source, out, Spec(), global_offset=100))
    assert [r.docid for r in records] == list(range(100, 105))
    assert [r.doc_id for r in records] == [str(i) for i in range(5)]
    summary = json.loads((out / 'encoding-summary.json').read_text())
    summary['chunks'].pop()
    (out / 'encoding-summary.json').write_text(json.dumps(summary))
    with pytest.raises(ValueError, match='coverage'):
        list(iter_shard_records(source, out, Spec(), global_offset=100))
