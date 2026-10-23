"""索引自检查工具：联调排错专用（离线组维护）。

    python -m offline.inspect fixtures/tiny_index info
    python -m offline.inspect fixtures/tiny_index term 心肌梗死
    python -m offline.inspect fixtures/tiny_index doc 0
"""

import sys

from .disk_index import DiskIndex


def main(argv: list) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 2
    idx = DiskIndex(argv[1])
    cmd, rest = argv[2], argv[3:]

    if cmd == "info":
        m = idx.manifest()
        print(f"dataset={m['dataset']}  n_docs={m['n_docs']}  avgdl={m['avgdl']:.2f}")
        print(f"analyzer={m['analyzer']['name']}@{m['analyzer']['version']}"
              f"  hash={m['analyzer']['config_hash'][:12]}…")
        print(f"vocab={len(list(idx.vocab()))} 个词项")
    elif cmd == "term" and rest:
        pl = idx.postings(rest[0])
        if pl is None:
            print(f"词 {rest[0]!r} 不在索引里")
            return 1
        print(f"df={len(pl)}")
        for docid, tf, positions in pl:
            print(f"  文档{docid}（{idx.doc(docid).doc_id}）tf={tf} 位置={positions}")
    elif cmd == "doc" and rest:
        d = idx.doc(int(rest[0]))
        print(f"doc_id={d.doc_id}  title={d.title}")
        print(d.text)
        print(f"tokens={idx.doc_length(d.docid)}")
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
