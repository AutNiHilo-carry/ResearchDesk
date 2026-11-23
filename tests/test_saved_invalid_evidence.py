"""Saved integrity failures remain failures during offline replay; injected labels only."""
import copy
import json
from pathlib import Path
import pytest
from researchdesk import judge_stability as stability, task_eval
from researchdesk.task_grading import TASK_PROMPT
from researchdesk.verification import SYSTEM_PROMPT_V2, digest
from researchdesk.verification import assess, prepare, replay_assessment
from pydantic import ValidationError
from test_task_eval import task_data, fixture_runner, RubricJudge, EvidenceJudge


def invalid_frame(kind='missing', factual=True):
    eid='E0000000000000000'
    units=[dict(unit_id='U1',line=1,text=f'Fixture assertion.[{eid}]',citations=[eid])]
    evidence,sources={},{}
    if kind=='tampered':
        evidence[eid]=dict(claim='Original fact',source_id='Sbad',snippet='Changed quote',location='fixture://doc')
        sources['Sbad']=dict(content='Original fact',location='fixture://doc',sha256='bad')
    payload,_=prepare(units,evidence,sources)
    def labels(_):
        return dict(units=[dict(unit_id='U1',factual=factual,reason='Injected fixture label',
            links=[dict(evidence_id=eid,verdict='supported' if factual else 'not_applicable',reason='Injected model label')])])
    return payload,assess(payload,labels)


@pytest.mark.parametrize('kind',['missing','tampered'])
@pytest.mark.parametrize('factual',[True,False])
def test_integrity_override_replays_without_mutation_or_false_support(kind,factual):
    payload,saved=invalid_frame(kind,factual)
    before=copy.deepcopy((payload,saved))
    checked=replay_assessment(payload,saved)
    assert checked==saved and (payload,saved)==before
    assert checked['units'][0]['links'][0]['verdict']=='invalid_evidence'
    assert checked['units'][0]['covered'] is False
    assert checked['metrics']['invalid_links']==1 and checked['metrics']['supported_links']==0
    assert checked['metrics']['evidence_coverage']==(0 if factual else None)


@pytest.mark.parametrize('mutation',['reason','metrics','covered','text','extra','link_id','false_support'])
def test_cached_integrity_override_still_requires_exact_recomputation(mutation):
    payload,saved=invalid_frame()
    changed=copy.deepcopy(saved)
    row=changed['units'][0]
    if mutation=='reason': row['links'][0]['reason']='Forged integrity explanation'
    elif mutation=='metrics': changed['metrics']['invalid_links']=0
    elif mutation=='covered': row['covered']=True
    elif mutation=='text': row['text']='Edited report'
    elif mutation=='extra': row['unexpected']='extra'
    elif mutation=='link_id': row['links'][0]['evidence_id']='Einvented'
    else: row['links'][0]['verdict']='supported'
    with pytest.raises(ValueError): replay_assessment(payload,changed)


def test_override_cannot_be_claimed_for_valid_evidence_or_returned_by_raw_model():
    payload,saved=invalid_frame()
    payload['evidence'][0]['integrity']='valid'
    with pytest.raises(ValueError): replay_assessment(payload,saved)
    raw=dict(units=[{k:saved['units'][0][k] for k in ('unit_id','factual','reason','links')}])
    with pytest.raises(ValidationError): assess(payload,lambda _:raw)


@pytest.mark.parametrize('contract',['legacy','body-length-v1'])
def test_saved_invalid_link_survives_full_batch_and_stability_replay(tmp_path,contract):
    data,tasks,documents=task_data()
    task=dict(tasks[0],generation_contract=contract)
    def runner(task,paths,directory,timeout):
        record=fixture_runner(task,paths,directory,timeout)
        run=Path(record['run_directory'])
        report=run/'draft.md'  # The verifier intentionally prefers the draft over the exported report.
        report.write_text(report.read_text(encoding='utf-8').split('\n## Evidence Ledger\n',1)[0]
                          +'\nMissing fact.[E0000000000000000]',encoding='utf-8')
        metadata=json.loads((run/'run.json').read_text(encoding='utf-8'))
        metadata.update(model='fixture',question=task['question']+task_eval.task_suffix(task))
        (run/'run.json').write_text(json.dumps(metadata),encoding='utf-8')
        record['question_sha256']=digest(metadata['question'])
        if contract!='legacy': record['generation_contract']=task_eval.generation_contract_spec(task)
        return record
    rubric=RubricJudge()
    rubric.prompt_version,rubric.prompt_sha256='research-task-rubric-v1',digest(TASK_PROMPT)
    def judge():
        result=EvidenceJudge()
        result.prompt_version,result.prompt_sha256='report-quote-v2',digest(SYSTEM_PROMPT_V2)
        return result
    evaluation=tmp_path/'evaluation'
    result=task_eval.evaluate_batch(data,[task],documents,evaluation,runner=runner,model_name='fixture',
                                    task_judge=rubric,evidence_judge=judge())
    assert result['status']=='complete' and result['task_success_rate']==0
    dataset=Path(__file__).parents[1]/'evals/research-tasks-v1.json'
    quality,binding=task_eval.saved_quality(evaluation,dataset,result)
    assert quality['metrics']['invalid_links']==1
    assert quality['metrics']['supported_links']==1 and quality['metrics']['citation_precision']==0.5
    payload,controls=stability.frozen_input(evaluation,dataset)
    assert controls['baseline_quality_sha256']==binding['quality_sha256']
    saved=tmp_path/'stability'
    stability.run_stability(payload,controls,saved,judge,repeats=2)
    before={p:p.read_bytes() for root in (evaluation,saved) for p in root.rglob('*') if p.is_file()}
    gate=stability.check_stability_result(evaluation,dataset,saved/'result.json',tmp_path/'checked')
    assert gate['status']=='passed' and gate['api_requests']==0  # Classification consistency, not task success.
    assert json.loads((evaluation/'result.json').read_text(encoding='utf-8'))['task_success_rate']==0
    assert {p:p.read_bytes() for root in (evaluation,saved) for p in root.rglob('*') if p.is_file()}==before
