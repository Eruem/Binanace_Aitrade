"""阶段 4:Kill Switch 测试(指导书 8.3)。"""

import pytest

from risk.kill_switch import KillSwitch, KillSwitchError


def test_default_disengaged():
    ks = KillSwitch()
    assert not ks.is_engaged


def test_engage_and_release_manual():
    ks = KillSwitch()
    ks.engage("紧急停止测试", source="manual")
    assert ks.is_engaged
    assert ks.state.source == "manual"
    ks.release("确认恢复")
    assert not ks.is_engaged
    assert ks.state.released_at is not None


def test_auto_pause_needs_manual_release():
    ks = KillSwitch()
    ks.auto_pause("行情接口崩溃")
    assert ks.is_engaged
    assert ks.state.source == "auto"
    # release 是唯一恢复路径(即人工操作)
    ks.release("人工确认恢复")
    assert not ks.is_engaged


def test_engage_is_idempotent():
    ks = KillSwitch()
    first = ks.engage("第一次", source="manual")
    second = ks.engage("第二次", source="manual")
    assert first.engaged_at == second.engaged_at
    assert second.reason == "第一次"


def test_unknown_source_rejected():
    ks = KillSwitch()
    with pytest.raises(KillSwitchError):
        ks.engage("x", source="llm")  # LLM 永远不能操作


def test_persists_across_instances(tmp_path):
    path = tmp_path / "ks.json"
    ks1 = KillSwitch(path)
    ks1.auto_pause("崩溃")
    ks2 = KillSwitch(path)  # 重启后仍生效
    assert ks2.is_engaged
    ks2.release("人工恢复")
    ks3 = KillSwitch(path)
    assert not ks3.is_engaged


def test_corrupt_state_file_engages(tmp_path):
    path = tmp_path / "ks.json"
    path.write_text("{not json", encoding="utf-8")
    ks = KillSwitch(path)
    assert ks.is_engaged  # 损坏 = 按已触发处理(宁可停)
