"""Export selected criteria with unchanged report bodies into the existing calibration format."""
import argparse
import hashlib
import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from .storage import atomic_text
from .task_eval import TASK_SUFFIX
from .task_grading_calibration import compile_cases
from .verification import VerificationError, digest, load_run


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


def export(pilot, targets_path, output):
    pilot = pilot.resolve()
    plan_path, result_path = pilot / 'plan.json', pilot / 'execution/result.json'
    plan, result, selection = read_json(plan_path), read_json(result_path), read_json(targets_path)
    if selection.get('version') != 'task-grading-replay-targets-v1':
        raise VerificationError('Unknown replay target format.')
    targets = [Target.model_validate(row) for row in selection['targets']]
    keys = [(t.arm, t.task_id, t.criterion_id) for t in targets]
    if not 1 <= len(keys) <= 8 or len(keys) != len(set(keys)):
        raise VerificationError('Replay needs 1..8 distinct targets.')
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
    tasks = {t['id']: t for t in plan['tasks']}
    cases, origins = [], []
    for target in targets:
        task = tasks[target.task_id]
        criterion = next(c for c in task['criteria'] if c['id'] == target.criterion_id)
        arm = result['arms'][target.arm]
        rows = [r for r in arm['tasks'] if r['task_id'] == target.task_id]
        if len(rows) != 1 or rows[0]['generation_status'] != 'completed':
            raise VerificationError('Target needs one completed source report.')
        record = rows[0]
        directory = Path(record['run_directory']).resolve()
        if not directory.is_relative_to(pilot / 'execution' / target.arm / 'tasks'):
            raise VerificationError('Source run escapes the selected pilot arm.')
        if record['question_sha256'] != digest(task['question'] + TASK_SUFFIX):
            raise VerificationError('Source report question changed.')
        report, _, _, metadata = load_run(directory)
        if record['model'] != plan['model'] or metadata['model'] != plan['model']:
            raise VerificationError('Source report model differs from plan.')
        if metadata.get('report_policy') != controls['report_policies'][target.arm]:
            raise VerificationError('Source report policy differs from selected arm.')
        report_path = directory / ('draft.md' if (directory / 'draft.md').is_file() else 'report.md')
        case_id = f'{target.arm}_{target.task_id}_{target.criterion_id}'
        cases.append(dict(id=case_id, question=task['question'], report=report,
                          documents=[plan['documents'][name]['content'] for name in task['documents']],
                          criterion=criterion, candidate_passed=target.candidate_passed))
        origins.append(dict(case_id=case_id, arm=target.arm, task_id=target.task_id,
                            criterion_id=target.criterion_id, report_body_sha256=digest(report),
                            report_file_sha256=file_digest(report_path),
                            run_manifest_sha256=file_digest(directory / 'run.json')))
    dataset = dict(version='task-grading-calibration-v1',
                   label_status='project_authored_candidates_pending_independent_review',
                   scope='targeted replay; unchanged extracted bodies; changed batch and criterion selection',
                   provenance=dict(pilot_plan_sha256=file_digest(plan_path),
                                   pilot_result_sha256=file_digest(result_path),
                                   targets_sha256=file_digest(targets_path),
                                   extraction='load_run body, excludes Evidence Ledger; reconstructed, not captured wire payload',
                                   reports=origins), cases=cases)
    compile_cases(dataset)  # Existing size/schema budgets; no truncation or rephrasing.
    output.mkdir(parents=True, exist_ok=False)
    atomic_text(output / 'dataset.json', json.dumps(dataset, ensure_ascii=False, indent=2))
    return dataset


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('pilot', type=Path)
    parser.add_argument('--targets', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        dataset = export(args.pilot, args.targets, args.output)
        print(f'Exported {len(dataset["cases"])} cases; zero API calls: {args.output / "dataset.json"}')
        return 0
    except Exception as error:
        print(f'Replay export failed ({type(error).__name__}).')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
