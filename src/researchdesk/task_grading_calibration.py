"""Small one-shot task-grader comparison; candidate references are never sent."""
import argparse
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field

from .ledger import now
from .storage import atomic_text
from .task_eval import Criterion
from .task_grading import TaskJudge, grade_tasks, task_prompt
from .verification import VerificationError, digest, failure_diagnostics


class Case(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    id: str = Field(pattern=r'^[a-z][a-z0-9_]{0,39}$')
    question: str = Field(min_length=1, max_length=2000)
    report: str = Field(min_length=1, max_length=4000)
    documents: list[str] = Field(min_length=1, max_length=4)
    criterion: Criterion
    candidate_passed: bool


def compile_cases(data):
    if data.get('version') != 'task-grading-calibration-v1':
        raise VerificationError('Unknown task grading calibration dataset.')
    cases = [Case.model_validate(row) for row in data['cases']]
    if not 1 <= len(cases) <= 8 or len({c.id for c in cases}) != len(cases):
        raise VerificationError('Calibration needs 1..8 unique cases.')
    payload = {'tasks': [dict(task_id=c.id, question=c.question, report=c.report,
                            documents=c.documents, criteria=[c.criterion.model_dump()]) for c in cases]}
    if len(json.dumps(payload, ensure_ascii=False)) > 30000:
        raise VerificationError('Calibration input too large.')
    return payload, {c.id: c.candidate_passed for c in cases}


def binding(plan):
    payload, references = compile_cases(plan['dataset'])
    if (plan.get('version') != 1 or payload != plan['payload'] or references != plan['references']
            or not isinstance(plan['model'], str) or not plan['model'].strip()):
        raise VerificationError('Invalid calibration controls.')
    return dict(input_sha256=digest(json.dumps(plan['dataset'], ensure_ascii=False, sort_keys=True)),
                model=plan['model'], prompts={v: digest(task_prompt(v)) for v in ('v1', 'v2')},
                logic={name: digest(Path(__file__).with_name(name).read_text(encoding='utf-8'))
                       for name in ('task_grading.py', 'task_grading_calibration.py')},
                max_sdk_requests=2, max_retries=0, order=['v1', 'v2'])


def prepare(dataset, output, *, model):
    data = json.loads(dataset.read_text(encoding='utf-8'))
    payload, references = compile_cases(data)
    plan = dict(version=1, created_at=now(), dataset=data, payload=payload, references=references, model=model,
                label_status='project_authored_candidates_pending_independent_review', human_review='pending')
    plan['controls'] = binding(plan)
    output.mkdir(parents=True, exist_ok=False)
    atomic_text(output / 'plan.json', json.dumps(plan, ensure_ascii=False, indent=2))
    return plan


def execute(output, *, factory=None):
    plan = json.loads((output / 'plan.json').read_text(encoding='utf-8'))
    if binding(plan) != plan['controls']:
        raise VerificationError('Frozen calibration changed; no requests allowed.')
    run = output / 'execution'
    run.mkdir(exist_ok=False)  # Never silently repeat a partially paid comparison.
    result = dict(status='running', started_at=now(), controls=plan['controls'], arms={},
                  mode='model_assisted' if factory is None else 'injected_offline',
                  label_status=plan['label_status'], human_review='pending', promotion='none', default_prompt='v1')
    def save():
        atomic_text(run / 'result.json', json.dumps(result, ensure_ascii=False, indent=2))
    save()
    try:
        for version in ('v1', 'v2'):
            if binding(plan) != plan['controls']:
                raise VerificationError('Calibration controls drifted.')
            judge = None
            arm = dict(status='running', prompt_sha256=digest(task_prompt(version)))
            result['arms'][version] = arm
            save()
            try:
                judge = (factory or TaskJudge)(plan['model'], prompt_version=version)
                # No candidate labels, dataset metadata or expected outputs in this request.
                grades = grade_tasks(plan['payload'], judge)
                matches = {key: row['rubric_passed'] == plan['references'][key] for key, row in grades.items()}
                arm.update(status='validated', grades=grades, candidate_agreement=matches,
                           candidate_agreement_rate=sum(matches.values()) / len(matches))
            except Exception as error:
                arm.update(status='failed', **failure_diagnostics(error), candidate_agreement_rate=None)
            finally:
                arm['usage'] = getattr(judge, 'usage', None)
                arm['raw_output'] = getattr(judge, 'last_output', None)
                save()
        result['status'] = ('complete' if all(a['status'] == 'validated' for a in result['arms'].values())
                            else 'incomplete_scoring')
    except BaseException as error:
        result.update(status='interrupted' if isinstance(error, KeyboardInterrupt) else 'failed',
                      error_type=type(error).__name__)
        raise
    finally:
        result['finished_at'] = now()
        save()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('prepare')
    p.add_argument('dataset', type=Path)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--model', default='deepseek-flash')
    e = sub.add_parser('execute')
    e.add_argument('plan', type=Path)
    args = parser.parse_args()
    try:
        if args.command == 'prepare':
            prepare(args.dataset, args.output, model=args.model)
            print(f'Prepared only; zero API calls: {args.output / "plan.json"}')
            return 0
        load_dotenv()
        if not os.getenv('DEEPSEEK_API_KEY', '').strip():
            print('Missing DEEPSEEK_API_KEY.')
            return 2
        result = execute(args.plan)
        print(f'Calibration status: {result["status"]}; {args.plan / "execution/result.json"}')
        return 0 if result['status'] == 'complete' else 3
    except Exception as error:
        print(f'Calibration failed ({type(error).__name__}).')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
