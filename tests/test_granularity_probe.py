import copy
import pytest
from researchdesk.claims import POLICY_VERSION, V2, V3
from researchdesk.ledger import Ledger
from researchdesk.granularity_probe import build
from researchdesk.verification import VerificationError


@pytest.fixture
def data(tmp_path):
    ledger = Ledger(tmp_path / "run")
    source = ledger.add_source("Fixture", "fixture://doc", "事实A。事实B。")
    ids = [ledger.add_evidence(s, source["id"], s)["id"] for s in ["事实A。", "事实B。"]]
    yield ledger, ids
    ledger.store.close()


def test_frozen_inputs_share_evidence_and_preserve_every_span(data):
    ledger, (a, b) = data
    body = f"事实A[{a}]。事实B[{b}]。"
    before = copy.deepcopy((ledger.evidence, ledger.sources))
    packet = build(body, ledger.evidence, ledger.sources)
    line, clause = packet["arms"]
    assert line["units"] == 1 and clause["units"] == 2
    assert line["payload"]["evidence"] == clause["payload"]["evidence"]
    assert "".join(u["text"] for u in clause["payload"]["units"]) == body
    assert [u["citations"] for u in clause["payload"]["units"]] == [[a], [b]]
    assert packet["metrics"] is None and packet["api_calls"] == 0
    assert (ledger.evidence, ledger.sources) == before


def test_trailing_pool_is_marked_as_candidate_binding(data):
    ledger, (a, _) = data
    packet = build(f"事实A；事实B。[{a}]", ledger.evidence, ledger.sources)
    assert packet["binding_counts"]["parent_final_candidate_pool"] == 2


@pytest.mark.parametrize("text", ["无效[Eunknown]", "```\n不可省略\n```", "\n".join(["句子"]*61)])
def test_invalid_or_over_budget_input_rejected(data, text):
    ledger, _ = data
    with pytest.raises(VerificationError):
        build(text, ledger.evidence, ledger.sources)


def test_unreferenced_corpus_change_invalidates_freeze(data):
    ledger, (a, _) = data
    body = f"事实A[{a}]。"
    before = build(body, ledger.evidence, ledger.sources)
    ledger.add_source("Extra", "fixture://extra", "另一个快照")
    after = build(body, ledger.evidence, ledger.sources)
    assert before["frozen_sha256"] != after["frozen_sha256"]


@pytest.mark.parametrize("policy", [POLICY_VERSION, V2, V3])
def test_separate_lines_bind_lock_and_recovery_evidence_independently(data, policy):
    ledger, _ = data
    lock = "分步备份在读取时需要短暂加锁。"
    recovery = "此摘记没有进行停电恢复试验。"
    source = ledger.add_source("Backup fixture", "fixture://backup", lock + recovery)
    lock_id = ledger.add_evidence(lock, source["id"], lock)["id"]
    recovery_id = ledger.add_evidence(recovery, source["id"], recovery)["id"]
    prefix = f"{lock}[{lock_id}]\n"
    mixed = prefix + f"不能保证无阻塞，尚未验证掉电恢复；摘记未执行停电试验。[{recovery_id}]"
    separated = prefix + f"不能保证无阻塞。[{lock_id}]\n尚未验证掉电恢复；摘记未执行停电试验。[{recovery_id}]"

    def arms(body):
        packet = build(body, ledger.evidence, ledger.sources, clause_policy=policy)
        assert packet["metrics"] is None and packet["api_calls"] == 0
        return [arm["payload"] for arm in packet["arms"]]

    mixed_lines, mixed_clauses = arms(mixed)
    split_lines, split_clauses = arms(separated)
    assert mixed_lines["evidence"] == split_lines["evidence"]
    assert mixed_clauses["evidence"] == split_clauses["evidence"]
    mixed_lock = mixed_clauses["units"][1]
    assert mixed_lock["text"] == "不能保证无阻塞，尚未验证掉电恢复；"
    assert mixed_lock["citation_binding"] == "parent_final_candidate_pool"
    assert mixed_lock["direct_citations"] == []
    assert mixed_lock["citations"] == [recovery_id]

    assert [row["citations"] for row in split_lines["units"][1:]] == [[lock_id], [recovery_id]]
    split_lock = split_clauses["units"][1]
    assert split_lock["text"] == f"不能保证无阻塞。[{lock_id}]"
    assert split_lock["citation_binding"] == "original_line"
    assert split_lock["citations"] == split_lock["direct_citations"] == [lock_id]
    for row in split_clauses["units"][2:]:
        assert row["parent_id"] == "U3"
        assert row["citations"] == [recovery_id]
