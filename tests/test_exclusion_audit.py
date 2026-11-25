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


def test_changed_report_keeps_unchanged_row_disagreements_in_the_paired_audit(gap_evaluation, tmp_path):
    from researchdesk.ledger import Ledger
    evaluation, dataset, _ = gap_evaluation
    baseline, candidate = comparison_fixture((evaluation, dataset, None), tmp_path)
    run = candidate / 'tasks/case/research' / ('a' * 32)
    report, evidence, sources, _ = load_run(run)
    first, duplicate, inference = report.splitlines()
    assert first == duplicate
    revised = '\n'.join([inference, first, duplicate, 'A new heading'])
    ledger = Ledger(run, resume=True)
    try:
        ledger.save_report(revised)
    finally:
        ledger.store.close()
    rows = split_units(revised)
    for row in rows:
        row.update(unit_id='case:' + row['unit_id'], task_id='case')
    payload, fingerprint = prepare(rows, evidence, sources)

    def saved(_):
        return dict(units=[dict(unit_id=row['unit_id'], factual=bool(row['citations']), reason='Fixture',
            links=[dict(evidence_id=eid, verdict='supported', reason='Fixture') for eid in row['citations']])
            for row in rows])

    score = assess(payload, saved)
    (candidate / 'citation-quality.json').write_text(json.dumps(score), encoding='utf-8')
    result_path = candidate / 'result.json'
    result = json.loads(result_path.read_bytes())
    result['quality_input_sha256'] = fingerprint
    result['tasks'][0]['quality'] = quality_metrics(score['units'])
    result_path.write_text(json.dumps(result), encoding='utf-8')
    before = {p: p.read_bytes() for root in (baseline, candidate) for p in root.rglob('*') if p.is_file()}

    audit = compare_evaluations(baseline, candidate)
    assert audit['changed_reports'] == 1 and audit['factuality_audit']['status'] == 'no_unchanged_reports'
    view = audit['changed_report_factuality_audit']
    assert view['status'] == 'review_required' and view['review_required_task_ids'] == ['case']
    check, = view['tasks']
    assert check['matched_units'] == 1 and len(check['ambiguous']) == 1
    change, = check['changes']
    assert (change['original_unit'], change['candidate_unit']) == ('case:U3', 'case:U1')
    assert check['ratio_increases_with_smaller_denominator'] == ['evidence_coverage']
    assert check['before']['covered_units'] == check['after']['covered_units'] == 2
    assert check['before']['evidence_coverage'] == 2/3 and check['after']['evidence_coverage'] == 1.0
    assert '已改报告中原样保留行的事实性检查：review_required' in audit_summary(audit)
    assert {p: p.read_bytes() for root in (baseline, candidate) for p in root.rglob('*') if p.is_file()} == before


def test_changed_report_does_not_pair_exact_text_with_an_unverified_quote():
    from researchdesk.evaluation_audit import changed_report_factuality
    eid = 'E' + 'a' * 16
    base = dict(unit_id='case:U1', task_id='case', text='Claim', citations=[eid])
    old = dict(base, factual=True, links=[dict(evidence_id=eid, verdict='supported')])
    new = dict(base, factual=False, links=[dict(evidence_id=eid, verdict='invalid_evidence')])
    result = changed_report_factuality([dict(task_id='case', report_changed=True)],
        {'case:U1': old}, {'case:U1': new},
        [dict(quoted_evidence=[dict(evidence_id=eid, integrity=integrity)]) for integrity in ('valid', 'invalid')])
    check, = result['tasks']
    assert check['status'] == 'unknown'
    assert check['unmatched_evidence_groups'] == 1 and check['matched_units'] == 0
    assert check['changes'] == []
    assert 'evidence_inputs_differ_or_invalid' in check['reasons']


def test_unknown_verdict_is_rejected_before_counting(gap_evaluation, tmp_path):
    baseline, candidate = pair_with_labels(gap_evaluation, tmp_path)
    path = candidate / 'citation-quality.json'
    quality = json.loads(path.read_text(encoding='utf-8'))
    quality['units'][0]['links'][0]['verdict'] = 'supported-ish'
    path.write_text(json.dumps(quality), encoding='utf-8')
    with pytest.raises(VerificationError):
        compare_evaluations(baseline, candidate)


@pytest.mark.parametrize('gate,expected', [(False, 0), (True, 3)])
def test_cli_keeps_diagnostics_compatible_but_explicit_gate_requires_review(gap_evaluation, tmp_path, monkeypatch, gate, expected):
    import sys
    from researchdesk import evaluation_audit as module
    baseline, candidate = pair_with_labels(gap_evaluation, tmp_path, 'case:U3')
    output = tmp_path / 'gate-output'
    args = ['evaluation_audit', str(baseline), str(candidate), '--output', str(output)]
    if gate:
        args.extend(['--gate', 'factuality'])
    monkeypatch.setattr(sys, 'argv', args)
    assert module.main() == expected
    if gate:
        verdict = json.loads((output / 'gate.json').read_text(encoding='utf-8'))
        assert verdict['status'] == 'review_required' and verdict['exit_code'] == 3
        audit = json.loads((output / 'result.json').read_text(encoding='utf-8'))
        from researchdesk.review import fingerprint
        assert verdict['audit_content_sha256'] == fingerprint(audit)
    else:
        assert not (output / 'gate.json').exists()


def test_gate_passes_unchanged_factuality_without_claiming_task_acceptance(gap_evaluation, tmp_path):
    from researchdesk.evaluation_audit import factuality_gate
    baseline, candidate = pair_with_labels(gap_evaluation, tmp_path)
    gate = factuality_gate(compare_evaluations(baseline, candidate))
    assert gate['status'] == 'passed' and gate['exit_code'] == 0
    assert gate['human_review'] == gate['final_acceptance'] == 'pending'
    assert gate['promotion'] == 'none'


def test_gate_missing_scores_is_unknown_not_passed(gap_evaluation, tmp_path):
    from researchdesk.evaluation_audit import factuality_gate
    baseline, candidate = pair_with_labels(gap_evaluation, tmp_path)
    (candidate / 'citation-quality.json').unlink()
    gate = factuality_gate(compare_evaluations(baseline, candidate))
    assert gate['status'] == 'unknown' and gate['exit_code'] == 3


@pytest.mark.parametrize('mutation', ['context', 'unfinished', 'legacy'])
def test_gate_requires_complete_same_context_audit(gap_evaluation, tmp_path, mutation):
    from researchdesk.evaluation_audit import factuality_gate
    baseline, candidate = pair_with_labels(gap_evaluation, tmp_path)
    audit = compare_evaluations(baseline, candidate)
    if mutation == 'context':
        audit['citation_input_identical'] = False
    elif mutation == 'unfinished':
        audit['candidate_status'] = 'scoring_failed'
    else:
        audit.pop('factuality_audit')
    gate = factuality_gate(audit)
    assert gate['status'] == 'unknown' and gate['exit_code'] == 3
