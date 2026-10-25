"""HTTP-only retrieval evaluation utilities.

The evaluator intentionally depends only on the public HTTP API and standard
library code.  Imports are resolved lazily so ``python -m evaluation.evaluate``
does not load the module twice through the package initializer.
"""

__all__ = [
    "EvaluationError",
    "EvaluationSummary",
    "evaluate_http",
    "evaluate_run",
    "load_qrels",
    "load_queries",
    "load_run",
]


def __getattr__(name):
    if name in __all__:
        from . import evaluate
        return getattr(evaluate, name)
    raise AttributeError(name)
