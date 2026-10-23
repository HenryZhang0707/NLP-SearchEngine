"""RRF：每路唯一 docid 排名；未命中该路贡献 0，同分按内部 docid。"""

import math
from collections import defaultdict
from typing import Sequence


def rrf(rankings: Sequence[Sequence[int]], c: float = 60.0) -> list[tuple[int, float]]:
    if not math.isfinite(c) or c <= 0:
        raise ValueError("RRF c 必须为有限正数")
    scores = defaultdict(float)
    for ranking in rankings:
        if len(ranking) != len(set(ranking)):
            raise ValueError("单路候选不得包含重复 docid")
        for rank, docid in enumerate(ranking, 1):
            scores[docid] += 1 / (c + rank)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))
