import pytest
from researchdesk.claims import POLICY_VERSION, V2, split_clauses, validate_anchors
from researchdesk.verification import VerificationError, split_units


@pytest.mark.parametrize("text", [
    "只有一个 WAL 文件[E1]。同一时刻只有一个写入者[E2]。",
    "该库只有1个连接[E1]；另一个库独立[E2]。",
    "读写可并行[E1]。但只有一个 WAL 文件，同一时间只能有一个写者[E2]。",
])
def test_narrow_quantity_exception_splits_with_exact_anchors(text):
    parents = split_units(text)
    assert len(split_clauses(parents)) == 1  # Existing version remains unchanged.
    rows = split_clauses(parents, policy_version=V2)
    validate_anchors(rows, parents)
    assert len(rows) == 2
    assert "".join(row["text"] for row in rows) == text
    assert [row["citations"] for row in rows] == [["E1"], ["E2"]]
    assert all(row["unitization_version"] == V2 for row in rows)


@pytest.mark.parametrize("text", [
    "只有当内存够用时才运行；否则退出。",
    "只有一个连接时才允许写入；另一个模式不同。",
    "只有一个 WAL 文件的情况下才可用；否则失败。",
    "只有一个写入者就能工作；否则等待。",
    "只有一个线程的时候执行；其余等待。",
    "只有授权用户可读；管理员另有规则。",
    "只有一个 WAL 文件；如果写入失败则重试。",
    "只有一个 WAL 文件；若断电则恢复。",
    "Only when memory is available can it run; otherwise stop.",
    "只有两个连接；第三个连接须等待。",
])
def test_true_conditions_and_unknown_quantity_patterns_remain_conservative(text):
    rows = split_clauses(split_units(text), policy_version=V2)
    assert len(rows) == 1 and rows[0]["text"] == text
    assert rows[0]["partition_reason"] == "conditional_scope_preserved"


def test_default_and_v1_explicit_are_identical():
    parents = split_units("只有一个 WAL 文件。另一事实。[E1]")
    assert split_clauses(parents) == split_clauses(parents, policy_version=POLICY_VERSION)


def test_unknown_policy_rejected_even_for_empty_input():
    with pytest.raises(VerificationError):
        split_clauses([], policy_version="v3")
