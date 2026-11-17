"""Subprocess-only provider fixtures around the real workflow CLI/runtime."""
import argparse
import json
from pathlib import Path
import sys
import os
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verdict', choices=['supported', 'unsupported'], required=True)
    parser.add_argument('--events', type=Path, required=True)
    parser.add_argument('--boundary', choices=['none', 'response', 'validated'], default='none')
    parser.add_argument('--marker', type=Path)
    parser.add_argument('workflow', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if (args.boundary == 'none') != (args.marker is None):
        parser.error('A parked boundary requires a marker, normal execution does not.')
    def guard(event, values):
        if event in {'socket.connect', 'socket.getaddrinfo'}:
            raise RuntimeError('Network forbidden in scripted workflow test')
    sys.addaudithook(guard)
    from researchdesk.fault_worker import scripted_model
    from researchdesk import agent, cli, repair, workflow
    from researchdesk.verification import SYSTEM_PROMPT_V2, digest
    from researchdesk.storage import atomic_text
    scripted_model()
    # Keep normal CLI/runtime/checkpoint/policy/repair wiring; replace only providers.
    cli.load_dotenv = workflow.load_dotenv = lambda: None
    def park():
        marker = args.marker.resolve()
        if not marker.is_relative_to(args.events.resolve().parent) or marker.exists():
            raise ValueError('Fault marker must be new and inside this test root')
        atomic_text(marker, json.dumps(dict(boundary=args.boundary, pid=os.getpid())))
        while True:
            time.sleep(1)
    def event(role):
        with args.events.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps({'fixture_provider': role}) + '\n')
    model_type = type(agent.ChatDeepSeek())
    generate = model_type._generate
    def counted_generate(self, *a, **k):
        event('generation')
        return generate(self, *a, **k)
    model_type._generate = counted_generate
    class Checker:
        prompt_version = 'report-quote-v2'
        prompt_sha256 = digest(SYSTEM_PROMPT_V2)
        usage = dict(input_tokens=1, output_tokens=1, total_tokens=2)
        def __init__(self, *a, **k):
            pass
        def __call__(self, payload):
            event('verification')
            output = {'units': [dict(unit_id=u['unit_id'], factual=True, reason='Scripted fixture label',
                links=[dict(evidence_id=eid, verdict=args.verdict, reason='Scripted fixture label')
                       for eid in u['citations']]) for u in payload['units']]}
            if args.boundary == 'response':
                park()  # Response object exists; usage/budget finish and quality persistence have not run.
            return output
    repair.ModelJudge = Checker
    if args.boundary == 'validated':
        original_assess = repair.assess
        def assess_then_park(*a, **k):
            result = original_assess(*a, **k)
            park()  # Output validation and usage commit finished; quality file is still absent.
            return result
        repair.assess = assess_then_park
    options = args.workflow[1:] if args.workflow[:1] == ['--'] else args.workflow
    sys.argv = ['researchdesk.workflow'] + options
    return workflow.main()


if __name__ == '__main__':
    raise SystemExit(main())
