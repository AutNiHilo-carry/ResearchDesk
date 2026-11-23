"""One-shot 2x2 judge experiment; never overwrites historical quality labels."""
import argparse
import json
from pathlib import Path
from dotenv import load_dotenv
from .source_context import CONTEXT_PROMPT, build_inputs, encode
from .report_eval import code_hashes
from .storage import RecoveryError, atomic_text
from .verification import SYSTEM_PROMPT_V2, ModelJudge, assess, digest, load_run

CONTEXT_RULES = CONTEXT_PROMPT[len(SYSTEM_PROMPT_V2):CONTEXT_PROMPT.index('ANY checkable')]
FACT_RULES = CONTEXT_PROMPT[CONTEXT_PROMPT.index('ANY checkable'):CONTEXT_PROMPT.index('An uncited factual')]
ARMS = ((False, False), (False, True), (True, False), (True, True))


def save(path, value):
    atomic_text(path, encode(value))


def create_plan(definitions, *, model='deepseek-flash'):
    if (not isinstance(definitions, list) or not 1 <= len(definitions) <= 3
            or not isinstance(model, str) or not model.strip() or len(model) > 100):
        raise RecoveryError('Use 1 to 3 samples and a bounded model name.')
    samples = []
    names = set()
    for definition in definitions:
        if (not isinstance(definition, dict) or set(definition) != {'name', 'run', 'body'}
                or not isinstance(definition['name'], str) or not definition['name'].strip()
                or len(definition['name']) > 100 or definition['name'] in names
                or definition['body'] not in {'selected', 'candidate'}):
            raise RecoveryError('Distinct sample names and explicit body selection are required.')
        names.add(definition['name'])
        run = Path(definition['run']).resolve()
        report, evidence, sources, _ = load_run(run)
        if definition['body'] == 'candidate':
            report = (run/'revisions/candidate.md').read_text(encoding='utf-8')
            state = json.loads((run/'repair-state.json').read_text(encoding='utf-8'))
            if state.get('candidate_sha256') != digest(report):
                raise RecoveryError('Candidate differs from its frozen repair hash.')
        packet = build_inputs(report, evidence, sources)
        arms = []
        for context, facts in ARMS:
            prompt = SYSTEM_PROMPT_V2 + (CONTEXT_RULES if context else '') + (FACT_RULES if facts else '')
            payload = packet['arms']['source-context-candidate-v1' if context else 'quote-only-v2']['payload']
            arms.append(dict(context=context, fact_hint=facts, prompt=prompt, payload=payload,
                             prompt_sha256=digest(prompt), input_sha256=digest(encode(payload))))
        samples.append(dict(name=definition['name'], run=str(run), body=definition['body'],
                            report_sha256=digest(report), arms=arms))
    return dict(version='context-factorial-v1', model=model, samples=samples,
                max_sdk_requests=4*len(samples), source_code_sha256=code_hashes(),
                human_review='pending', promotion='none', order='sample-major; 00,01,10,11',
                repetitions=1, retries=0)


def prepare_plan(definitions, output, *, model='deepseek-flash'):
    output = output.resolve()
    for definition in definitions:
        if output.is_relative_to(Path(definition['run']).resolve()):
            raise RecoveryError('Plan output must be outside source runs.')
    plan = create_plan(definitions, model=model)
    output.mkdir(parents=True, exist_ok=False)
    save(output/'plan.json', plan)
    return plan


def validate_plan(directory):
    plan = json.loads((directory/'plan.json').read_text(encoding='utf-8'))
    definitions = [{key: sample[key] for key in ('name', 'run', 'body')} for sample in plan['samples']]
    if plan != create_plan(definitions, model=plan['model']):
        raise RecoveryError('Frozen source, prompt, payload, budget or code changed.')
    return plan


def live_judge(model, arm):
    judge = ModelJudge(model, prompt_version='v2')
    judge.system_prompt = arm['prompt']
    judge.prompt_sha256 = arm['prompt_sha256']
    judge.prompt_version = 'context-factorial-v1'
    return judge


def execute_plan(directory, *, judge_factory=live_judge, credentials=None):
    plan = validate_plan(directory)
    output = directory/'execution'
    output.mkdir(exist_ok=False)  # Uncertain outcomes cannot be retried via this plan.
    result = dict(version=plan['version'], status='started', rows=[], sdk_attempts=0,
                  max_sdk_requests=plan['max_sdk_requests'], human_review='pending', promotion='none')
    save(output/'result.json', result)
    if credentials is not None:
        credentials()
    for sample in plan['samples']:
        for arm in sample['arms']:
            row = dict(sample=sample['name'], context=arm['context'], fact_hint=arm['fact_hint'],
                       status='started', usage=None)
            result['rows'].append(row)
            save(output/'result.json', result)
            judge = None
            try:
                judge = judge_factory(plan['model'], arm)
                result['sdk_attempts'] += 1
                save(output/'result.json', result)
                quality = assess(arm['payload'], judge)
                quality['metric_scope'] = ('candidate quote plus same-source body support' if arm['context']
                                           else 'model-estimated line units; strict single-quote full support')
                row.update(status='complete', quality=quality)
            except Exception as error:
                row.update(status='failed', error_type=type(error).__name__)
            finally:
                if judge is not None:
                    row.update(usage=getattr(judge, 'usage', None), raw_output=getattr(judge, 'last_output', None),
                               raw_text=getattr(judge, 'last_text', None))
                save(output/'result.json', result)
    result['status'] = 'complete' if all(row['status']=='complete' for row in result['rows']) else 'incomplete_scoring'
    usages = [row['usage'] for row in result['rows']]
    known = [usage['total_tokens'] for usage in usages if isinstance(usage, dict) and type(usage.get('total_tokens')) is int]
    result['known_returned_tokens'] = sum(known)
    result['total_tokens'] = sum(known) if len(known)==len(usages) else None
    save(output/'result.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'execute'])
    parser.add_argument('path', type=Path, help='Sample JSON for prepare; plan directory for execute')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if (args.action=='prepare') != (args.output is not None):
        parser.error('Only prepare requires --output.')
    try:
        if args.action=='prepare':
            plan = prepare_plan(json.loads(args.path.read_text(encoding='utf-8')), args.output)
            print(f"Zero API preparation; execution capped at {plan['max_sdk_requests']} SDK requests, not a monetary cap.")
        else:
            result = execute_plan(args.path.resolve(), credentials=lambda: load_dotenv(Path.cwd()/'.env'))
            print(f"Experiment {result['status']}; SDK attempts {result['sdk_attempts']}; no promotion.")
            return 0 if result['status']=='complete' else 3
    except (RecoveryError, ValueError, OSError, KeyError, TypeError):
        print('Experiment refused; no automatic retry.')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
