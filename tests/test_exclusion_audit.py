"""Mechanical scoring-denominator diagnostics; scripted labels are not human truth."""
import copy
import json
import pytest
from researchdesk.evaluation_audit import compare_evaluations, audit_summary
from researchdesk.verification import load_run, split_units, prepare, assess, VerificationError
from researchdesk.task_eval import quality_metrics, task_success
from test_review import review_case, gap_evaluation, comparison_fixture


def pair_with_labels(gap_evaluation, tmp_path, excluded=None, partial=False):
    evaluation, dataset, _ = gap_evaluation
    baseline, candidate = comparison_fixture((evaluation, dataset, None), tmp_path)
    run = candidate / 'tasks/case/research' / ('a' * 32)
    report, evidence, sources, _ = load_run(run)
    units = split_units(report)
    for row in units:
        row.update(unit_id='case:' + row['unit_id'], task_id='case')
    payload, _ = prepare(units, evidence, sources)
    old = json.loads((candidate / 'citation-quality.json').read_text(encoding='utf-8'))
    labels = [{key: copy.deepcopy(row[key]) for key in ('unit_id', 'factual', 'reason', 'links')}
              for row in old['units']]
    for row in labels:
        if excluded == 'all' or row['unit_id'] == excluded:
            row['factual'] = False
            for link in row['links']:
                link['verdict'] = 'not_applicable'
        if partial and row['unit_id'] == 'case:U1':
            row['links'][0]['verdict'] = 'partial'
    def saved(_): return dict(units=labels)
    saved.prompt_version = old['prompt_version']
    saved.prompt_sha256 = old['judge_prompt_sha256']
    quality = assess(payload, saved)
    (candidate / 'citation-quality.json').write_text(json.dumps(quality), encoding='utf-8')
    path = candidate / 'result.json'
    result = json.loads(path.read_text(encoding='utf-8'))
    record = result['tasks'][0]
    record['quality'] = quality_metrics(quality['units'])
    record['success'] = task_success(record, record['rubric'], record['quality'], result['thresholds'])
    result['task_success_rate'] = int(record['success'])
    path.write_text(json.dumps(result), encoding='utf-8')
    return baseline, candidate


@pytest.mark.parametrize('excluded,coverage,increased', [('case:U3', 1.0, True), ('case:U1', 0.5, False), ('all', None, False)])
def test_unchanged_report_exclusion_exposes_counts_and_requires_review(gap_evaluation, tmp_path, excluded, coverage, increased):
    baseline, candidate = pair_with_labels(gap_evaluation, tmp_path, excluded)
    before = {p: p.read_bytes() for d in (baseline, candidate) for p in d.rglob('*') if p.is_file()}
    audit = compare_evaluations(baseline, candidate)
    view = audit['factuality_audit']
    assert view['status'] == 'review_required'
    row = view['tasks'][0]
    assert row['before'] == dict(factual_units=3, covered_units=2, evidence_coverage=2/3)
    assert row['after']['evidence_coverage'] == coverage
    assert row['coverage_increased_with_fewer_factual_units'] is increased
    assert row['excluded_units']
    assert view['coverage_increase_task_ids'] == (['case'] if increased else [])
    assert 'review_required' in audit_summary(audit)
    assert {p: p.read_bytes() for d in (baseline, candidate) for p in d.rglob('*') if p.is_file()} == before


def test_verdict_change_without_fact_exclusion_is_not_denominator_warning(gap_evaluation, tmp_path):
    baseline, candidate = pair_with_labels(gap_evaluation, tmp_path, partial=True)
    path = candidate / 'citation-quality.json'
    stale = json.loads(path.read_text(encoding='utf-8'))
    stale['units'][0]['covered'] = True
    stale['metrics']['evidence_coverage'] = 1
    path.write_text(json.dumps(stale), encoding='utf-8')
    view = compare_evaluations(baseline, candidate)['factuality_audit']
    assert view['status'] == 'no_factuality_change'
    assert view['coverage_increase_task_ids'] == []
    assert view['tasks'][0]['excluded_units'] == []
    assert view['tasks'][0]['after']['factual_units'] == 3
    assert view['tasks'][0]['after']['covered_units'] == 1


def test_missing_labels_do_not_imply_no_exclusion(gap_evaluation, tmp_path):
    baseline, candidate = pair_with_labels(gap_evaluation, tmp_path)
    (candidate / 'citation-quality.json').unlink()
    view = compare_evaluations(baseline, candidate)['factuality_audit']
    assert view['status'] == 'unavailable' and view['tasks'] == []


def test_unknown_verdict_is_rejected_before_counting(gap_evaluation, tmp_path):
    baseline, candidate = pair_with_labels(gap_evaluation, tmp_path)
    path = candidate / 'citation-quality.json'
    quality = json.loads(path.read_text(encoding='utf-8'))
    quality['units'][0]['links'][0]['verdict'] = 'supported-ish'
    path.write_text(json.dumps(quality), encoding='utf-8')
    with pytest.raises(VerificationError):
        compare_evaluations(baseline, candidate)
