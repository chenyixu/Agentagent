"""候选选择识别：用户没说"要哪个"时，运行时不能替他决定。

这是"不超卖"的上游一环。替用户默认选第一个，会在用户没有确认的情况下真实占用
技师与房间的时段；而把"第二天下午三点"误读成"第二个候选"更糟——它会把一次纯
时间的表达变成一次选人。因此这里逐条钉住识别边界（设计稿 §2.1）。
"""

from __future__ import annotations

import pytest

from appointment.agent.deterministic import detect_candidate_selection

pytestmark = pytest.mark.invariant


@pytest.mark.parametrize(
    ("text", "count", "expected"),
    [
        # 明确的序号选择
        ("第一个可以", 3, 0),
        ("就第二个吧", 3, 1),
        ("选第三个", 3, 2),
        ("第 2 个", 3, 1),
        ("我要第十个", 12, 9),
        # 只有一个候选时，"可以"没有第二种解释
        ("可以", 1, 0),
        ("好的", 1, 0),
        ("行", 1, 0),
    ],
)
def test_explicit_selection_is_recognised(text, count, expected):
    assert detect_candidate_selection(text, candidate_count=count) == expected


@pytest.mark.parametrize(
    ("text", "count"),
    [
        # 时间表达不能变成序号选择
        ("明天下午三点", 3),
        ("第二天下午三点可以吗", 3),
        ("后天下午", 5),
        # 序号越界：宁可追问，不要就近取一个
        ("第五个", 3),
        ("第0个", 3),
        # 多个候选时的"可以"有歧义，不能当成"要第一个"
        ("可以", 3),
        ("好的", 3),
        # 没有选择语义
        ("你们几点关门", 3),
        ("", 3),
        ("价格是多少", 3),
    ],
)
def test_ambiguous_or_unrelated_input_is_not_a_selection(text, count):
    assert detect_candidate_selection(text, candidate_count=count) is None


def test_no_candidates_means_no_selection():
    assert detect_candidate_selection("第一个", candidate_count=0) is None
