"""Subprocess-only provider fixtures around the real workflow CLI/runtime."""
import argparse
import json
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verdict', choices=['supported', 'unsupported'], required=True)
    parser.add_argument('--events', type=Path, required=True)
    parser.add_argument('workflow', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    def guard(event, values):
        if event in {'socket.connect', 'socket.getaddrinfo'}:
            raise RuntimeError('Network forbidden in scripted workflow test')
    sys.addaudithook(guard)
    from researchdesk.fault_worker import scripted_model
    from researchdesk import agent, cli, repair, workflow
    from researchdesk.verification import SYSTEM_PROMPT_V2, digest
    scripted_model()
    # Keep normal CLI/runtime/checkpoint/policy/repair wiring; replace only providers.
    cli.load_dotenv = workflow.load_dotenv = lambda: None
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
            return {'units': [dict(unit_id=u['unit_id'], factual=True, reason='Scripted fixture label',
                links=[dict(evidence_id=eid, verdict=args.verdict, reason='Scripted fixture label')
                       for eid in u['citations']]) for u in payload['units']]}
    repair.ModelJudge = Checker
    options = args.workflow[1:] if args.workflow[:1] == ['--'] else args.workflow
    sys.argv = ['researchdesk.workflow'] + options
    return workflow.main()


if __name__ == '__main__':
    raise SystemExit(main())
