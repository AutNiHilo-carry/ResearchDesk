"""Fixed developer references; injected replies test detection, never model accuracy."""
import json
from pathlib import Path
import pytest
from researchdesk.evaluation import benchmark_inputs, evaluate
from researchdesk.verification import prepare

DATASET=Path(__file__).parents[1]/'evals/factuality-mixed-dev-v1.json'


def data():
    return json.loads(DATASET.read_text(encoding='utf-8'))


def injected_reply(payload, references, *, drop_mixed=False, drop_advice=False):
    rows=[]
    for unit in payload['units']:
        reference=references[unit['unit_id']]
        factual=reference['factual']
        if drop_mixed and unit['unit_id']=='case01': factual=False
        if drop_advice and unit['unit_id']=='case05': factual=False
        rows.append(dict(unit_id=unit['unit_id'],factual=factual,reason='Injected test response, not a human opinion',
            links=[dict(evidence_id=e,verdict=reference['verdicts'][e] if factual else 'not_applicable',reason='Injected only') for e in unit['citations']]))
    return dict(units=rows)


def test_case_preserves_explained_original_and_no_gold_enters_payload():
    dataset=data()
    units,evidence,sources,references=benchmark_inputs(dataset)
    payload,_=prepare(units,evidence,sources)
    assert dataset['explained_case_ids']==['case01']
    assert dataset['label_status']=='agent_authored_candidate_references_not_human_gold'
    assert dataset['independent_acceptance_eligible'] is False and dataset['human_review']=='pending'
    assert units[0]['text']=='保存以端点开始执行为界；未开始执行的情形资料未说明，证据不足。'
    assert units[0]['citations']==[] and references['case01']['factual'] is True
    serialized=json.dumps(payload,ensure_ascii=False)
    assert all(word not in serialized for word in ('expected_factual','expected_verdicts','reference_reason','explained_case_ids','development_focus'))
    assert set(payload)=={'units','evidence'}
    assert all(row['integrity']=='valid' for row in payload['evidence'])


@pytest.mark.parametrize('drop_mixed,drop_advice,expected',[(True,False,['case01']),(False,True,['case05'])])
def test_dropped_factual_premise_is_detected_even_when_response_schema_is_valid(drop_mixed,drop_advice,expected):
    dataset=data()
    _,_,_,references=benchmark_inputs(dataset)
    result=evaluate(dataset,lambda p:injected_reply(p,references,drop_mixed=drop_mixed,drop_advice=drop_advice))
    assert [r['case_id'] for r in result['benchmark']['factual_disagreements']]==expected
    assert not result['benchmark']['gate_passed']
    if drop_mixed:
        # Link labels may all agree while a missing factual denominator must fail the dev gate.
        assert result['benchmark']['semantic_link_accuracy']==1
        assert result['benchmark']['factual_classification_accuracy']==pytest.approx(5/6)


def test_uncited_fact_enters_denominator_and_abstention_controls_stay_nonfactual():
    dataset=data()
    _,_,_,references=benchmark_inputs(dataset)
    result=evaluate(dataset,lambda p:injected_reply(p,references))
    assert result['metrics']['factual_units']==3
    assert result['metrics']['covered_units']==1
    assert result['metrics']['evidence_coverage']==pytest.approx(1/3)
    assert result['benchmark']['gate_passed']  # Injected reference adherence only, not real-model success.
    assert [row['unit_id'] for row in result['units'] if not row['factual']]==['case02','case04','case06']


def test_fixture_regression_never_loads_credentials_or_calls_network(monkeypatch):
    import socket
    from researchdesk import verification
    monkeypatch.setattr(socket,'socket',lambda *a,**k:pytest.fail('No network'))
    monkeypatch.setattr(verification,'ModelJudge',lambda *a,**k:pytest.fail('No model'))
    dataset=data()
    _,_,_,references=benchmark_inputs(dataset)
    assert evaluate(dataset,lambda p:injected_reply(p,references))['benchmark']['label_status']==dataset['label_status']
