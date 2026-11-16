import copy
import pytest
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
