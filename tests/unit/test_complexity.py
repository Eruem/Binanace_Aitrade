"""阶段 1:复杂度评分测试。"""

from llm.complexity import ComplexityInput, ComplexityScorer


def test_zero_when_no_factors():
    s = ComplexityScorer()
    assert s.score(ComplexityInput()) == 0
    assert not s.should_upgrade(ComplexityInput())


def test_weights():
    s = ComplexityScorer(threshold=99)
    assert s.score(ComplexityInput(multi_timeframe=True)) == 2
    assert s.score(ComplexityInput(abnormal_volatility=True)) == 2
    assert s.score(ComplexityInput(data_conflict=True)) == 3
    assert s.score(ComplexityInput(data_incomplete=True)) == 2
    assert s.score(ComplexityInput(agent_conflict=True)) == 3
    assert s.score(ComplexityInput(multi_strategy_comparison=True)) == 1
    assert s.score(ComplexityInput(multi_timeframe=True, data_conflict=True)) == 5


def test_threshold_upgrade():
    s = ComplexityScorer(threshold=3)
    assert s.should_upgrade(ComplexityInput(data_conflict=True))  # 3
    assert not s.should_upgrade(ComplexityInput(multi_timeframe=True))  # 2


def test_extra_factors_ignored_if_invalid():
    s = ComplexityScorer()
    ctx = ComplexityInput(extra_factors={"bogus": -5, "also_bogus": "x", "ok": 2})
    assert s.score(ctx) == 2


def test_bool_is_not_extra_int():
    # bool 是 int 子类,必须被拒绝
    s = ComplexityScorer()
    assert s.score(ComplexityInput(extra_factors={"flag": True})) == 0
