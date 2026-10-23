"""查询扩展注册点（在线组）。

扩展只影响打分排序，绝不能破坏用户显式写的 AND / NOT / 引号约束；
扩展词必须出现在语料词表内（防 query drift）。
RM3 / SLM 的实现是 E1 之后的工作，先留桩。
"""

from contracts.reader import IndexReader


class ExpanderNotReady(NotImplementedError):
    pass


def expand_none(reader: IndexReader, query: str, terms: list) -> list:
    """不扩展。"""
    return []


def expand_rm3(reader: IndexReader, query: str, terms: list) -> list:
    """TODO(在线组)：首轮 BM25 取前 M 篇 -> 词项概率模型 -> (1-α)q + α·R 再检索。
    要求：固定输入固定输出；扩展词 ∈ vocab()；扩展词权重总和 ≤ 原始词权重。"""
    raise ExpanderNotReady("RM3 未实现，见 DESIGN.md §四行表第 3 行")


def expand_slm(reader: IndexReader, query: str, terms: list) -> list:
    """TODO(在线组)：Qwen3-0.6B 生成同义改写 -> 词表校验 -> 落盘缓存（评测时禁实时调模型）。"""
    raise ExpanderNotReady("SLM 未实现，见 DESIGN.md §四行表第 4 行")


EXPANDERS = {"none": expand_none, "rm3": expand_rm3, "slm": expand_slm}
