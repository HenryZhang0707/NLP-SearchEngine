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
