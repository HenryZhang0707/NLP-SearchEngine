"""Optional cross-encoder control on a fixed prefix of hybrid candidates."""

import math


class RerankUnavailable(RuntimeError):
    """The selected cross-encoder did not produce a usable ranking."""


class HybridReranker:
    def __init__(self, scorer, depth: int):
        if type(depth) is not int or not 1 <= depth <= 40:
            raise ValueError("hybrid rerank depth must be between 1 and 40")
        self.scorer = scorer
        self.depth = depth

    @property
    def identity(self) -> dict:
        return {"depth": self.depth, "candidate_source": "hybrid_rrf",
                "model": dict(self.scorer.identity)}

    def rerank(self, query, ranked, read_document):
        prefix = ranked[:self.depth]
        documents = [read_document(docid) for docid, _ in prefix]
        texts = [doc.title + "\n" + doc.text for doc in documents]
        try:
            scores = self.scorer.score(query, texts)
            if (len(scores) != len(prefix)
                    or any(type(score) not in (int, float) or not math.isfinite(score)
                           for score in scores)):
                raise ValueError("invalid cross-encoder scores")
        except (OSError, TypeError, RuntimeError, ValueError, OverflowError) as error:
            raise RerankUnavailable("cross-encoder scoring failed") from error
        ordered = sorted(zip(scores, documents, prefix),
                         key=lambda row: (-row[0], row[1].doc_id))
        # Preserve first-stage RRF scores. CE scores rank this prefix only.
        return ([item for _, _, item in ordered] + ranked[len(prefix):],
                {"depth": self.depth, "scored": len(prefix)})
