"""Injected graph outputs exercise publication failure reporting, not research quality."""
import json
from pathlib import Path
import pytest
from langchain_core.messages import AIMessage
from researchdesk import cli,recovery,task_eval
from researchdesk.final_body_gate import verify_rejection
from researchdesk.storage import RecoveryError
from researchdesk.tools import ResearchTools
from test_task_eval import EvidenceJudge, RubricJudge, task_data


@pytest.fixture
def runner(monkeypatch):
    monkeypatch.setattr(cli,'load_dotenv',lambda:None)
    monkeypatch.setenv('DEEPSEEK_API_KEY','fixture')
    def graph(ledger,initial,limits,model_name,*,allowed_tools):
        source=next(iter(ledger.sources.values()))
        quote=source['content'].splitlines()[0]
        evidence=ledger.add_evidence('Fixture',source['id'],quote)
        return {'messages':[AIMessage(content='长'*201+f"[{evidence['id']}]")]}
    monkeypatch.setattr(recovery,'run_graph',graph)
    def command(args,cwd,log,timeout,environment):
        code=cli.main(args[3:],model_name=environment['DEEPSEEK_MODEL'])
        return dict(returncode=code,timed_out=False,elapsed_seconds=0)
    monkeypatch.setattr(task_eval,'run_command',command)
    return command


def invocation(tmp_path):
    path=tmp_path/'doc.md'
    path.write_text('Fixture fact.',encoding='utf-8')
    return dict(id='sample',question='固定研究问题',final_body_limit=200),[path]


def test_verified_rejection_is_separate_from_generation_failure_and_has_no_publication(tmp_path,runner):
    task,paths=invocation(tmp_path)
    result=task_eval.run_task(task,paths,tmp_path/'task',240)
    assert result['generation_status']=='publication_rejected'
    assert result['publication']['status']=='rejected' and result['constraints']['counted_chars']==201
    assert not (Path(result['run_directory'])/'report.md').exists()
    state=task_eval.assessment(result,'complete')
    assert state['scoring_status']=='not_scored' and state['model_gate']=='unknown'
    assert task_eval.application_assessment(result,'complete')['application_gate']=='failed'


@pytest.mark.parametrize('mutation',['receipt','candidate','orphan_report'])
def test_inconsistent_rejection_artifacts_do_not_become_a_trusted_gate_failure(tmp_path,runner,monkeypatch,mutation):
    task,paths=invocation(tmp_path)
    def command(*args):
        result=runner(*args)
        run=next((tmp_path/'task/research').iterdir())
        if mutation=='receipt':
            path=run/'final-body-check.json'
            value=json.loads(path.read_text(encoding='utf-8'))
            value['body_constraints']['counted_chars']=1
            path.write_text(json.dumps(value),encoding='utf-8')
        elif mutation=='candidate': (run/'draft.md').write_text('replaced',encoding='utf-8')
        else: (run/'report.md').write_text('unexpected publication',encoding='utf-8')
        return result
    monkeypatch.setattr(task_eval,'run_command',command)
    result=task_eval.run_task(task,paths,tmp_path/'task',240)
    assert result['generation_status']=='invalid_artifacts'
    assert 'publication' not in result


def test_timeout_remains_timeout_even_if_rejection_marker_exists(tmp_path,runner,monkeypatch):
    task,paths=invocation(tmp_path)
    def command(*args): return dict(runner(*args),timed_out=True)
    monkeypatch.setattr(task_eval,'run_command',command)
    assert task_eval.run_task(task,paths,tmp_path/'task',240)['generation_status']=='timed_out'


def test_rejected_candidates_are_not_sent_to_graders_and_summary_explains_gate(tmp_path,runner):
    data,tasks,documents=task_data()
    def run(task,paths,directory,timeout):
        return task_eval.run_task(dict(task,final_body_limit=200),paths,directory,timeout)
    def forbidden(*args): pytest.fail('Unpublished candidates cannot reach semantic graders')
    result=task_eval.evaluate_batch(data,tasks[:1],documents,tmp_path/'evaluation',runner=run,
                                    task_judge=forbidden,evidence_judge=forbidden)
    assert result['status']=='complete' and result['task_success_rate']==0
    assert result['tasks'][0]['generation_status']=='publication_rejected'
    assert result['tasks'][0]['success'] is False
    assert result['evaluator_usage']==[] and result['evaluator_total_tokens']==0
    text=task_eval.summary(result)
    assert 'publication_rejected' in text and 'publication=blocked' in text and '201/200' in text


@pytest.mark.parametrize('limit',[True,0,100001,'200'])
def test_invalid_final_limit_rejected_before_directory_creation(tmp_path,limit):
    with pytest.raises(ValueError):
        task_eval.run_task(dict(id='sample',question='q',final_body_limit=limit),[],tmp_path/'task',240)
    assert not (tmp_path/'task').exists()


def test_rejection_check_is_readonly(tmp_path,runner):
    import hashlib
    task,paths=invocation(tmp_path)
    result=task_eval.run_task(task,paths,tmp_path/'task',240)
    run=Path(result['run_directory'])
    def hashes(): return {str(p.relative_to(run)):hashlib.sha256(p.read_bytes()).hexdigest() for p in run.rglob('*') if p.is_file()}
    before=hashes()
    assert verify_rejection(run)==result['publication']
    assert before==hashes()


def test_normalized_rejected_candidate_is_verified_against_actual_checked_body(tmp_path,runner,monkeypatch):
    def graph(ledger,*args,**kwargs):
        source=next(iter(ledger.sources.values()))
        evidence=ledger.add_evidence('Fixture',source['id'],source['content'])
        return {'messages':[AIMessage(content='长'*201+f"［{evidence['id']}］")]}
    monkeypatch.setattr(recovery,'run_graph',graph)
    task,paths=invocation(tmp_path)
    result=task_eval.run_task(task,paths,tmp_path/'task',240)
    assert result['generation_status']=='publication_rejected'
    assert result['constraints']['body_sha256']==result['publication']['check']['body_constraints']['body_sha256']


def test_citation_rejection_is_not_reported_as_length_failure(tmp_path,runner,monkeypatch):
    monkeypatch.setattr(recovery,'run_graph',lambda *a,**k:{'messages':[AIMessage(content='事实。[E0000000000000000]')]})
    task,paths=invocation(tmp_path)
    result=task_eval.run_task(task,paths,tmp_path/'task',240)
    assert result['generation_status']=='publication_rejected'
    assert result['constraints']['passed']
    assert result['publication']['check']['citation_structure']=='failed'


@pytest.mark.parametrize('change',['successful_exit','missing_runtime_flag'])
def test_runtime_policy_or_exit_contradictions_are_not_accepted(tmp_path,runner,monkeypatch,change):
    task,paths=invocation(tmp_path)
    def command(args,*other):
        if change=='missing_runtime_flag':
            args=list(args)
            at=args.index('--final-body-limit')
            del args[at:at+2]
        result=runner(args,*other)
        return dict(result,returncode=0) if change=='successful_exit' else result
    monkeypatch.setattr(task_eval,'run_command',command)
    assert task_eval.run_task(task,paths,tmp_path/'task',240)['generation_status']=='invalid_artifacts'


@pytest.mark.parametrize('options', [
    {'reuse_checked_draft_on_overflow': 'true'},
    {'reuse_checked_draft_on_overflow': True, 'final_body_limit': 200},
    {'reuse_checked_draft_on_overflow': True, 'draft_inspection_limit': 200},
])
def test_invalid_overflow_configuration_rejected_before_generation(tmp_path, options):
    with pytest.raises(ValueError, match='Overflow fallback'):
        task_eval.run_task(dict(id='sample', question='q', **options), [], tmp_path / 'task', 240)
    assert not (tmp_path / 'task').exists()


@pytest.mark.parametrize('mode', ['final_candidate', 'checked_draft'])
def test_batch_records_and_rechecks_completed_publication_path(tmp_path, runner, monkeypatch, mode):
    from researchdesk.review import build_packet

    def graph(ledger, initial, limits, model_name, *, allowed_tools):
        source = next(iter(ledger.sources.values()))
        quote = source['content'].splitlines()[0]
        evidence = ledger.add_evidence('Fixture fact.', source['id'], quote)
        citation = f"[{evidence['id']}]"
        checked = f'Fixture fact. {citation}'
        tools = ResearchTools(ledger, '', allowed_tools=allowed_tools)
        result = tools.executor.execute('inspect_report', {'report': checked})
        assert result['body_constraints']['passed']
        final = checked if mode == 'final_candidate' else '长' * 201 + citation
        return {'messages': [AIMessage(content=final)]}

    monkeypatch.setattr(recovery, 'run_graph', graph)
    data, tasks, documents = task_data()
    selected = dict(tasks[0], draft_inspection_limit=200, final_body_limit=200,
                    reuse_checked_draft_on_overflow=True)
    evaluation = tmp_path / 'evaluation'
    scored_reports = []

    def rubric(payload):
        scored_reports.extend(row['report'] for row in payload['tasks'])
        return RubricJudge()(payload)

    result = task_eval.evaluate_batch(data, [selected], documents, evaluation, model_name='fixture',
                                      task_judge=rubric, evidence_judge=EvidenceJudge())
    row, = result['tasks']
    assert row['generation_status'] == 'completed'
    assert row['publication_path']['mode'] == mode
    assert row['final_body_policy']['overflow_policy'] == 'reuse-checked-draft-v1'
    run = Path(row['run_directory'])
    assert scored_reports == [(run / 'draft.md').read_text(encoding='utf-8')]
    report = task_eval.summary(result)
    if mode == 'checked_draft':
        assert row['publication_path']['selection'] == json.loads(
            (run / 'overflow-selection.json').read_text(encoding='utf-8'))
        assert '复用通过预检的草稿' in report
    else:
        assert row['publication_path'] == {'mode': 'final_candidate'}
        assert '直接发布通过检查的最终稿' in report

    dataset = Path(__file__).parents[1] / 'evals' / 'research-tasks-v1.json'
    build_packet(evaluation, dataset)
    if mode == 'checked_draft':
        path = run / 'overflow-candidate.md'
        original = path.read_bytes()
        path.write_text('tampered', encoding='utf-8')
        with pytest.raises(RecoveryError):
            build_packet(evaluation, dataset)
        path.write_bytes(original)
    row.pop('publication_path')
    (evaluation / 'result.json').write_text(json.dumps(result), encoding='utf-8')
    with pytest.raises(ValueError, match='Saved publication path'):
        build_packet(evaluation, dataset)
