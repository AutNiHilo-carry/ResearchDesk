"""Record self-declared factuality opinions offline; never replace scores or promote policy."""
import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal
from pydantic import BaseModel, ConfigDict
from .judge_stability import check_stability_result, frozen_input, payload_hash
from .storage import atomic_text
from .verification import VerificationError


class Opinion(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    unit_id: str
    factual: bool | None
    atomic_claims: list[str]
    reason: str


class Submission(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    version: Literal['stability-review-template-v1']
    context_sha256: str
    reviewer: str
    reviewed_at: str
    status: Literal['pending', 'partial', 'complete']
    units: list[Opinion]


def record_review(evaluation, dataset, source, bundle, review, output):
    evaluation, source, bundle, review, output = [Path(p).resolve() for p in
                                                 (evaluation, source, bundle, review, output)]
    if output.exists() or any(output.is_relative_to(p) for p in (evaluation, source.parent, bundle)):
        raise VerificationError('Use a new output outside the evaluation, stability run and review bundle.')
    names = ('audit.json', 'gate.json', 'review-context.json', 'review-template.json', 'review.md')
    paths = [bundle/name for name in names]
    if any(p.resolve().parent != bundle for p in paths):
        raise VerificationError('Review bundle files must stay inside the bundle.')
    original = {p: p.read_bytes() for p in paths}
    submitted = review.read_bytes()
    # Rebuild from the actual source instead of trusting a mutually edited context/hash pair.
    with TemporaryDirectory(prefix='researchdesk-stability-review-') as scratch:
        checked = Path(scratch)/'checked'
        check_stability_result(evaluation, dataset, source, checked)
        for path in paths:
            expected = (checked/path.name).read_bytes()
            same = (original[path] == expected if path.suffix == '.md'
                    else payload_hash(json.loads(original[path])) == payload_hash(json.loads(expected)))
            if not same:
                raise VerificationError('Review bundle differs from the current bound source.')
    context = json.loads(original[bundle/'review-context.json'])
    audit = json.loads(original[bundle/'audit.json'])
    form = Submission.model_validate_json(submitted)
    if form.context_sha256 != payload_hash(context) or not form.reviewer.strip():
        raise VerificationError('Reviewer or review context binding is missing.')
    stamp = datetime.fromisoformat(form.reviewed_at.replace('Z', '+00:00'))
    if stamp.utcoffset() is None:
        raise VerificationError('Review timestamp requires a timezone.')
    ids = [row.unit_id for row in form.units]
    expected_ids = [row['unit_id'] for row in context['payload']['units']]
    if len(ids) != len(set(ids)) or set(ids) != set(expected_ids):
        raise VerificationError('Every scored unit must appear exactly once; unknown remains null.')
    for row in form.units:
        if (row.factual is not None and not row.reason.strip()) or any(not claim.strip() for claim in row.atomic_claims):
            raise VerificationError('Known labels require reasons; atomic claims must not be blank.')
    known = sum(row.factual is not None for row in form.units)
    completeness = 'complete' if known == len(ids) else 'partial'
    if form.status != 'pending' and form.status != completeness:
        raise VerificationError('Declared completion differs from the actual known labels.')
    _, controls = frozen_input(evaluation, dataset)
    if (controls != audit['controls'] or hashlib.sha256(source.read_bytes()).hexdigest() != audit['source_result_sha256']
            or hashlib.sha256(source.with_name('input.json').read_bytes()).hexdigest() != audit['input_file_sha256']
            or review.read_bytes() != submitted or any(p.read_bytes() != raw for p, raw in original.items())):
        raise VerificationError('Review or bound source changed during import.')
    result = dict(version='stability-review-record-v1', status='human_opinion_recorded', completeness=completeness,
        exit_code=0 if completeness == 'complete' else 3, reviewer=form.reviewer, reviewed_at=form.reviewed_at,
        identity='self_declared_not_authenticated', context_sha256=payload_hash(context),
        audit_content_sha256=context['audit_content_sha256'], submitted_review_sha256=hashlib.sha256(submitted).hexdigest(),
        known_units=known, unknown_units=len(ids)-known, total_units=len(ids),
        units=[row.model_dump() for row in form.units], api_requests=0, accuracy=None,
        final_acceptance='pending', promotion='none', original_scores_modified=False,
        scope='recorded_factuality_opinions_not_independent_gold_or_task_acceptance')
    output.mkdir(parents=True, exist_ok=False)
    atomic_text(output/'submitted-review.json', submitted.decode('utf-8'))
    atomic_text(output/'result.json', json.dumps(result, ensure_ascii=False, indent=2))
    atomic_text(output/'summary.md', '# Stability factuality review recorded\n\n'
        + f'Completeness: {completeness}; known: {known}/{len(ids)}; unknown: {len(ids)-known}.\n\n'
        + 'Identity is self-declared. Zero API requests. Historical scores unchanged.\n'
        + 'Unknown labels remain null. No accuracy, task acceptance or policy promotion assigned.\n')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('evaluation', type=Path)
    for name in ('dataset', 'source', 'bundle', 'review', 'output'):
        parser.add_argument('--'+name, type=Path, required=True)
    args = parser.parse_args()
    try:
        result = record_review(args.evaluation, args.dataset, args.source, args.bundle, args.review, args.output)
        print(f"Factuality opinion recorded: {result['completeness']}; unknown units: {result['unknown_units']}; zero API calls.")
        return result['exit_code']
    except Exception as error:
        print(f'Review rejected ({type(error).__name__}); no accepted opinion written.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
