"""Export selected criteria with unchanged report bodies into the existing calibration format."""
import argparse
import hashlib
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field

from .storage import atomic_text
from .task_eval import TASK_SUFFIX
from .task_grading_calibration import compile_cases
from .task_grading import TaskJudge, grade_tasks, task_prompt
from .verification import VerificationError, digest, load_run, failure_diagnostics
from .ledger import now


class Target(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    arm: str = Field(pattern=r'^v[12]$')
    task_id: str = Field(pattern=r'^[a-z][a-z0-9_]{0,39}$')
    criterion_id: str = Field(pattern=r'^[a-z][a-z0-9_]{0,39}$')
    candidate_passed: bool


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def file_digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_pilot(pilot):
    pilot = pilot.resolve()
    plan_path, result_path = pilot / 'plan.json', pilot / 'execution/result.json'
    plan, result = read_json(plan_path), read_json(result_path)
    controls = plan['controls']
    frozen_input = {key: plan[key] for key in ('data', 'tasks', 'documents', 'model', 'max_model_calls', 'timeout')}
    if (plan.get('version') != 1 or result['controls'] != controls
            or digest(json.dumps(frozen_input, ensure_ascii=False, sort_keys=True)) != controls['input_sha256']):
        raise VerificationError('Pilot inputs differ from recorded execution controls.')
    if digest(TASK_SUFFIX) != controls['task_suffix_sha256']:
        raise VerificationError('Original task suffix changed.')
    for name in ('task_eval.py', 'verification.py'):
        if digest(Path(__file__).with_name(name).read_text(encoding='utf-8')) != controls['source_code_sha256'][name]:
            raise VerificationError('Original request reconstruction code changed.')
    for document in plan['documents'].values():
        if digest(document['content']) != document['sha256']:
            raise VerificationError('Frozen pilot document changed.')
    return plan, result


def source_report(pilot, plan, result, arm_name, task):
    rows = [r for r in result['arms'][arm_name]['tasks'] if r['task_id'] == task['id']]
    if len(rows) != 1 or rows[0]['generation_status'] != 'completed':
        raise VerificationError('Target needs one completed source report.')
    record = rows[0]
    directory = Path(record['run_directory']).resolve()
    if not directory.is_relative_to(pilot.resolve() / 'execution' / arm_name / 'tasks'):
        raise VerificationError('Source run escapes the selected pilot arm.')
    if record['question_sha256'] != digest(task['question'] + TASK_SUFFIX):
        raise VerificationError('Source report question changed.')
    report, _, _, metadata = load_run(directory)
    if record['model'] != plan['model'] or metadata['model'] != plan['model']:
        raise VerificationError('Source report model differs from plan.')
    if metadata.get('report_policy') != plan['controls']['report_policies'][arm_name]:
        raise VerificationError('Source report policy differs from selected arm.')
    report_path = directory / ('draft.md' if (directory / 'draft.md').is_file() else 'report.md')
    return report, dict(arm=arm_name, task_id=task['id'], report_body_sha256=digest(report),
                       report_file_sha256=file_digest(report_path), run_manifest_sha256=file_digest(directory / 'run.json'))


def export(pilot, targets_path, output):
    pilot = pilot.resolve()
    plan, result = load_pilot(pilot)
    selection = read_json(targets_path)
    if selection.get('version') != 'task-grading-replay-targets-v1':
        raise VerificationError('Unknown replay target format.')
    targets = [Target.model_validate(row) for row in selection['targets']]
    keys = [(t.arm, t.task_id, t.criterion_id) for t in targets]
    if not 1 <= len(keys) <= 8 or len(keys) != len(set(keys)):
        raise VerificationError('Replay needs 1..8 distinct targets.')
    tasks = {t['id']: t for t in plan['tasks']}
    cases, origins = [], []
    for target in targets:
        task = tasks[target.task_id]
        criterion = next(c for c in task['criteria'] if c['id'] == target.criterion_id)
        report, origin = source_report(pilot, plan, result, target.arm, task)
        case_id = f'{target.arm}_{target.task_id}_{target.criterion_id}'
        cases.append(dict(id=case_id, question=task['question'], report=report,
                          documents=[plan['documents'][name]['content'] for name in task['documents']],
                          criterion=criterion, candidate_passed=target.candidate_passed))
        origins.append(dict(origin, case_id=case_id, criterion_id=target.criterion_id))
    dataset = dict(version='task-grading-calibration-v1',
                   label_status='project_authored_candidates_pending_independent_review',
                   scope='targeted replay; unchanged extracted bodies; changed batch and criterion selection',
                   provenance=dict(pilot_plan_sha256=file_digest(pilot / 'plan.json'),
                                   pilot_result_sha256=file_digest(pilot / 'execution/result.json'),
                                   targets_sha256=file_digest(targets_path),
                                   extraction='load_run body, excludes Evidence Ledger; reconstructed, not captured wire payload',
                                   reports=origins), cases=cases)
    compile_cases(dataset)  # Existing size/schema budgets; no truncation or rephrasing.
    output.mkdir(parents=True, exist_ok=False)
    atomic_text(output / 'dataset.json', json.dumps(dataset, ensure_ascii=False, indent=2))
    return dataset


def stability_binding(plan):
    if (plan.get('version') != 'task-grading-stability-v1' or type(plan.get('repeats')) is not int
            or not 2 <= plan['repeats'] <= 3 or not isinstance(plan.get('model'), str) or not plan['model'].strip()
            or set(plan.get('batches', {})) != {'v1', 'v2'}):
        raise VerificationError('Invalid stability plan.')
    for payload in plan['batches'].values():
        tasks = payload['tasks']
        if not 1 <= len(tasks) <= 3 or len({t['task_id'] for t in tasks}) != len(tasks):
            raise VerificationError('Stability batch must retain 1..3 unique tasks.')
        for task in tasks:
            ids = [c['id'] for c in task['criteria']]
            if not ids or len(ids) != len(set(ids)):
                raise VerificationError('Invalid stability criteria.')
        if len(json.dumps(payload, ensure_ascii=False)) > 60000:
            raise VerificationError('Full batch exceeds existing grader input limit.')
    frozen = {key: plan[key] for key in ('batches', 'provenance', 'model', 'repeats')}
    schedule = [dict(batch=batch, repeat=repeat, prompt=prompt)
                for batch in ('v1', 'v2') for repeat in range(1, plan['repeats'] + 1)
                for prompt in (('v1', 'v2') if repeat % 2 else ('v2', 'v1'))]
    return dict(input_sha256=digest(json.dumps(frozen, ensure_ascii=False, sort_keys=True)),
                prompts={v: digest(task_prompt(v)) for v in ('v1', 'v2')},
                logic={name: digest(Path(__file__).with_name(name).read_text(encoding='utf-8'))
                       for name in ('task_grading.py', 'task_grading_replay.py')},
                schedule=schedule, max_sdk_requests=len(schedule), max_retries=0, temperature=0)


def prepare_stability(pilot, output, *, repeats=2):
    pilot = pilot.resolve()
    source, result = load_pilot(pilot)
    batches, origins = {}, []
    for arm in ('v1', 'v2'):
        rows = []
        for task in source['tasks']:
            report, origin = source_report(pilot, source, result, arm, task)
            rows.append(dict(task_id=task['id'], question=task['question'], criteria=task['criteria'], report=report,
                             documents=[source['documents'][name]['content'] for name in task['documents']]))
            origins.append(origin)
        batches[arm] = {'tasks': rows}
    plan = dict(version='task-grading-stability-v1', created_at=now(), model=source['model'], repeats=repeats,
                batches=batches, provenance=dict(pilot_plan_sha256=file_digest(pilot / 'plan.json'),
                    pilot_result_sha256=file_digest(pilot / 'execution/result.json'), reports=origins,
                    scope='full original rubric batches reconstructed; no captured wire payload or correctness labels'))
    plan['controls'] = stability_binding(plan)
    output.mkdir(parents=True, exist_ok=False)
    atomic_text(output / 'plan.json', json.dumps(plan, ensure_ascii=False, indent=2))
    return plan


def stability_rows(plan, attempts):
    rows = []
    for batch, payload in plan['batches'].items():
        for prompt in ('v1', 'v2'):
            calls = [a for a in attempts if a['batch'] == batch and a['prompt'] == prompt]
            for task in payload['tasks']:
                for criterion in task['criteria']:
                    values = []
                    for call in calls:
                        grade = call.get('grades', {}).get(task['task_id'], {})
                        item = next((c for c in grade.get('criteria', []) if c['criterion_id'] == criterion['id']), None)
                        values.append(item['passed'] if item else None)
                    complete = len(values) == plan['repeats'] and all(type(v) is bool for v in values)
                    rows.append(dict(batch=batch, prompt=prompt, task_id=task['task_id'], criterion_id=criterion['id'],
                                     observed=values, consistent=(len(set(values)) == 1) if complete else None))
    return rows


def execute_stability(output, *, factory=None):
    plan = read_json(output / 'plan.json')
    if stability_binding(plan) != plan['controls']:
        raise VerificationError('Frozen stability plan changed.')
    run = output / 'execution'
    run.mkdir(exist_ok=False)
    result = dict(status='running', started_at=now(), controls=plan['controls'], attempts=[],
                  mode='model_assisted' if factory is None else 'injected_offline',
                  human_review='pending', default_prompt='v1', promotion='none', correctness='not_measured')
    def save():
        result['criterion_stability'] = stability_rows(plan, result['attempts'])
        atomic_text(run / 'result.json', json.dumps(result, ensure_ascii=False, indent=2))
    save()
    try:
        for slot in plan['controls']['schedule']:
            if stability_binding(plan) != plan['controls']:
                raise VerificationError('Stability controls drifted between requests.')
            row, judge = dict(slot, status='started'), None
            result['attempts'].append(row)
            save()  # A crash after dispatch is retained; this directory cannot be re-executed.
            try:
                judge = (factory or TaskJudge)(plan['model'], prompt_version=slot['prompt'])
                row['grades'] = grade_tasks(plan['batches'][slot['batch']], judge)
                row['status'] = 'validated'
            except Exception as error:
                row.update(status='failed', **failure_diagnostics(error))
            finally:
                row['usage'] = getattr(judge, 'usage', None)
                row['raw_output'] = getattr(judge, 'last_output', None)
                save()
        result['status'] = ('complete' if all(a['status'] == 'validated' for a in result['attempts'])
                            else 'incomplete_scoring')
    except BaseException as error:
        result.update(status='interrupted' if isinstance(error, KeyboardInterrupt) else 'failed', error_type=type(error).__name__)
        raise
    finally:
        result['finished_at'] = now()
        save()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('pilot', type=Path)
    parser.add_argument('--targets', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--prepare-stability', action='store_true')
    parser.add_argument('--execute-stability', action='store_true')
    parser.add_argument('--repeats', type=int)
    args = parser.parse_args()
    if args.execute_stability and (args.prepare_stability or args.targets or args.output or args.repeats is not None):
        parser.error('Execution accepts only a frozen plan directory.')
    if not args.execute_stability and (not args.output or (not args.prepare_stability and not args.targets)):
        parser.error('Preparation/export requires output and targeted export requires targets.')
    if (args.prepare_stability and args.targets) or (args.repeats is not None and not args.prepare_stability):
        parser.error('Use repeats only with full-batch preparation, without targets.')
    try:
        if args.execute_stability:
            load_dotenv()
            if not os.getenv('DEEPSEEK_API_KEY', '').strip():
                print('Missing DEEPSEEK_API_KEY.')
                return 2
            result = execute_stability(args.pilot)
            print(f'Stability status: {result["status"]}; {args.pilot / "execution/result.json"}')
            return 0 if result['status'] == 'complete' else 3
        if args.prepare_stability:
            plan = prepare_stability(args.pilot, args.output, repeats=args.repeats if args.repeats is not None else 2)
            print(f'Prepared only; zero API calls; maximum SDK requests: {plan["controls"]["max_sdk_requests"]}')
            return 0
        dataset = export(args.pilot, args.targets, args.output)
        print(f'Exported {len(dataset["cases"])} cases; zero API calls: {args.output / "dataset.json"}')
        return 0
    except Exception as error:
        print(f'Replay export failed ({type(error).__name__}).')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
