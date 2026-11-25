import json
import shutil
from pathlib import Path
import pytest
from pydantic import ValidationError
from researchdesk.ledger import Ledger
from researchdesk.review import (build_packet, candidate_packet, export_review, fingerprint,
                                 import_candidate_review, import_review)
from researchdesk.task_eval import evaluate_batch, load_taskset
from researchdesk.verification import VerificationError


@pytest.fixture
def review_case(tmp_path):
    dataset = tmp_path / "dataset.json"
    (tmp_path / "source.md").write_text("Document fact.", encoding="utf-8")
    dataset.write_text(json.dumps(dict(version="research-tasks-v1", thresholds=dict(
        citation_precision=0.9, evidence_coverage=0.6), tasks=[dict(id="case", question="Question",
        documents=["source.md"], criteria=[dict(id="fact", description="Mention the fact.")])])), encoding="utf-8")
    data, tasks, docs = load_taskset(dataset)
    def runner(task, paths, directory, timeout):
        ledger = Ledger(directory / "research" / ("a" * 32))
        source = ledger.add_source("Source", str(paths[0]), "Document fact.", "local")
        evidence = ledger.add_evidence("Document fact.", source["id"], "Document fact.")
        ledger.save_report(f"Document fact.[{evidence['id']}]")
        (ledger.directory / "run.json").write_text(json.dumps(dict(status="completed")))
        ledger.store.close()
        return dict(task_id="case", generation_status="completed", run_directory=str(ledger.directory),
                    model="fixture", generation_metrics=dict(provider_requests=0))
    def rubric(payload):
        return {"tasks": [dict(task_id="case", criteria=[dict(criterion_id="fact", passed=True,
            report_quote="Document fact.", reason="Fixture")])]}
    def judge(payload):
        return {"units": [dict(unit_id=row["unit_id"], factual=True, reason="Fixture",
            links=[dict(evidence_id=eid, verdict="supported", reason="Fixture") for eid in row["citations"]])
            for row in payload["units"]]}
    evaluation = tmp_path / "evaluation"
    evaluate_batch(data, tasks, docs, evaluation, runner=runner, model_name="fixture",
                   task_judge=rubric, evidence_judge=judge)
    bundle = tmp_path / "review-bundle"
    export_review(evaluation, dataset, bundle)
    return evaluation, dataset, bundle


def filled_review(bundle):
    review = json.loads((bundle / "review.json").read_text(encoding="utf-8"))
    review.update(reviewer="Fixture reviewer (test only)", reviewed_at="2026-11-10T12:00:00+08:00")
    for task in review["tasks"]:
        for row in task["units"]:
            row.update(factual=True, reason="Fixture decision")
            for link in row["links"]:
                link.update(verdict="supported", reason="Fixture decision")
        for criterion in task["criteria"]:
            criterion.update(passed=True, report_quote="Document fact.", reason="Fixture decision")
    return review


def submit(case, review, output):
    evaluation, dataset, bundle = case
    path = bundle / "filled-test-only.json"
    path.write_text(json.dumps(review), encoding="utf-8")
    return import_review(evaluation, dataset, bundle, path, output)


def test_export_hides_model_decisions_and_blank_labels_are_not_success(review_case, tmp_path):
    evaluation, dataset, bundle = review_case
    content = json.loads((bundle / "packet.json").read_text(encoding="utf-8"))["content"]
    assert "task_success_rate" not in content and "rubric_passed" not in str(content)
    review = json.loads((bundle / "review.json").read_text(encoding="utf-8"))
    assert review["tasks"][0]["units"][0]["factual"] is None
    with pytest.raises(ValidationError):
        import_review(evaluation, dataset, bundle, bundle / "review.json", tmp_path / "output")
    assert not (tmp_path / "output").exists()


def test_import_records_human_disagreement_without_overwriting_model(review_case, tmp_path):
    evaluation, _, bundle = review_case
    before = (evaluation / "result.json").read_bytes()
    review = filled_review(bundle)
    review["tasks"][0]["units"][0]["links"][0]["verdict"] = "partial"
    result = submit(review_case, review, tmp_path / "accepted")
    assert result["human_task_success_rate"] == 0 and result["model_task_success_rate"] == 1
    assert result["disagreements"][0]["field"] == "verdict"
    assert result["promotion"] == "no_automatic_promotion"
    assert (evaluation / "result.json").read_bytes() == before
    with pytest.raises(FileExistsError):
        submit(review_case, review, tmp_path / "accepted")


@pytest.mark.parametrize("mutation", ["omit_unit", "duplicate_task", "invent_anchor", "wrong_packet", "no_timezone"])
def test_incomplete_or_unbound_review_is_rejected(review_case, tmp_path, mutation):
    review = filled_review(review_case[2])
    if mutation == "omit_unit":
        review["tasks"][0]["units"] = []
    elif mutation == "duplicate_task":
        review["tasks"].append(review["tasks"][0])
    elif mutation == "invent_anchor":
        review["tasks"][0]["criteria"][0]["report_quote"] = "Invented quote"
    elif mutation == "wrong_packet":
        review["packet_sha256"] = "wrong"
    else:
        review["reviewed_at"] = "2026-11-10T12:00:00"
    with pytest.raises(VerificationError):
        submit(review_case, review, tmp_path / "rejected")
    assert not (tmp_path / "rejected").exists()


def test_changed_evaluation_input_invalidates_review(review_case, tmp_path):
    evaluation, dataset, bundle = review_case
    review = filled_review(bundle)
    run = evaluation / "tasks" / "case" / "research" / ("a" * 32)
    (run / "draft.md").write_text("Changed report.", encoding="utf-8")
    with pytest.raises(VerificationError, match="changed"):
        submit(review_case, review, tmp_path / "rejected")


@pytest.fixture
def candidate_case(review_case, tmp_path):
    from researchdesk.delivery import blank_review
    evaluation, dataset, _ = review_case
    original, = build_packet(evaluation, dataset)["tasks"]
    body = original["report"].replace("Document fact.", "Document fact. Scope was clarified.")
    candidate = tmp_path / "candidate.md"
    candidate.write_text(body, encoding="utf-8")
    packet, _ = candidate_packet(original, body)
    bundle = tmp_path / "candidate-review"
    bundle.mkdir()
    packet_hash = fingerprint(packet)
    (bundle / "packet.json").write_text(json.dumps(dict(sha256=packet_hash, content=packet)), encoding="utf-8")
    (bundle / "review.json").write_text(json.dumps(blank_review(packet, packet_hash)), encoding="utf-8")
    submitted = tmp_path / "opinion.json"
    submitted.write_text(json.dumps(filled_review(bundle)), encoding="utf-8")
    return evaluation, dataset, bundle, candidate, submitted


def test_candidate_opinion_records_only_unpublished_scope(candidate_case, tmp_path, monkeypatch):
    import socket
    import sys
    from researchdesk import review
    evaluation, dataset, bundle, candidate, submitted = candidate_case
    before = {p: p.read_bytes() for root in (evaluation, bundle) for p in root.rglob("*") if p.is_file()}
    output = tmp_path / "recorded"
    monkeypatch.setattr(sys, "argv", ["review", "import-candidate", str(evaluation), "--dataset", str(dataset),
        "--bundle", str(bundle), "--candidate", str(candidate), "--review", str(submitted), "--output", str(output)])
    monkeypatch.setattr(socket, "socket", lambda *a, **k: pytest.fail("Candidate review must stay offline"))
    assert review.main() == 0
    result = json.loads((output / "result.json").read_bytes())
    assert result["publication"] == "unpublished" and result["independent_review"] == "not_verified"
    assert result["rubric"]["rubric_passed"] and result["quality"]["citation_precision"] == 1.0
    assert (output / "submitted-review.json").read_bytes() == submitted.read_bytes()
    assert before == {p: p.read_bytes() for root in (evaluation, bundle) for p in root.rglob("*") if p.is_file()}


@pytest.mark.parametrize("problem", ["blank", "changed_body", "source_quote", "wrong_anchor"])
def test_candidate_import_rejects_mismatched_material_or_opinions(candidate_case, tmp_path, problem):
    evaluation, dataset, bundle, candidate, submitted = candidate_case
    if problem == "blank":
        submitted = bundle / "review.json"
    elif problem == "changed_body":
        candidate.write_bytes(candidate.read_bytes() + b" changed")
    elif problem == "source_quote":
        path = bundle / "packet.json"
        envelope = json.loads(path.read_bytes())
        evidence = envelope["content"]["tasks"][0]["evidence"]
        next(iter(evidence.values()))["snippet"] = "Modified evidence."
        envelope["sha256"] = fingerprint(envelope["content"])
        path.write_text(json.dumps(envelope), encoding="utf-8")
    else:
        opinion = json.loads(submitted.read_bytes())
        opinion["tasks"][0]["criteria"][0]["report_quote"] = "Absent phrase."
        submitted.write_text(json.dumps(opinion), encoding="utf-8")
    output = tmp_path / "rejected"
    with pytest.raises((VerificationError, ValidationError)):
        import_candidate_review(evaluation, dataset, bundle, candidate, submitted, output)
    assert not output.exists()


def test_transfer_cases_have_same_thresholds_and_new_task_ids():
    root = Path(__file__).parents[1] / "evals"
    old, old_tasks, _ = load_taskset(root / "research-tasks-v1.json")
    new, new_tasks, _ = load_taskset(root / "transfer-tasks-v1.json")
    assert new["thresholds"] == old["thresholds"]
    assert not {row["id"] for row in old_tasks} & {row["id"] for row in new_tasks}


def comparison_fixture(review_case, tmp_path):
    evaluation = review_case[0]
    result = json.loads((evaluation / "result.json").read_text(encoding="utf-8"))
    result.update(task_grader_prompt_sha256="fixture-rubric", citation_judge_prompt_sha256="fixture-citation")
    (evaluation / "result.json").write_text(json.dumps(result), encoding="utf-8")
    candidate = tmp_path / "candidate-evaluation"
    shutil.copytree(evaluation, candidate)
    return evaluation, candidate


def test_comparison_flags_same_report_regrading_as_disagreement(review_case, tmp_path):
    from researchdesk.evaluation_audit import compare_evaluations
    baseline, candidate = comparison_fixture(review_case, tmp_path)
    result = json.loads((candidate / "result.json").read_text(encoding="utf-8"))
    result["tasks"][0]["success"] = False
    result["task_success_rate"] = 0
    (candidate / "result.json").write_text(json.dumps(result), encoding="utf-8")
    quality_path = candidate / "citation-quality.json"
    quality = json.loads(quality_path.read_text(encoding="utf-8"))
    quality["units"][0]["links"][0]["verdict"] = "partial"
    quality_path.write_text(json.dumps(quality), encoding="utf-8")
    audit = compare_evaluations(baseline, candidate)
    assert audit["unchanged_reports"] == 1 and audit["changed_reports"] == 0
    assert audit["citation_input_identical"] is True
    assert audit["unchanged_success_disagreements"] == ["case"]
    assert audit["unchanged_report_citation_disagreements"][0]["after"] == "partial"


def test_comparison_refuses_changed_controls(review_case, tmp_path):
    from researchdesk.evaluation_audit import compare_evaluations
    baseline, candidate = comparison_fixture(review_case, tmp_path)
    result = json.loads((candidate / "result.json").read_text(encoding="utf-8"))
    result["thresholds"]["citation_precision"] = 0.1
    (candidate / "result.json").write_text(json.dumps(result), encoding="utf-8")
    with pytest.raises(VerificationError, match="controls differ"):
        compare_evaluations(baseline, candidate)


def subset_case(review_case):
    evaluation, dataset, _ = review_case
    data = json.loads(dataset.read_text(encoding='utf-8'))
    extra = dict(id='unselected', question='Unused question', documents=['unused.md'],
                 criteria=[dict(id='extra', description='Unused criterion')])
    (dataset.parent / 'unused.md').write_text('Unused document.', encoding='utf-8')
    data['tasks'].insert(0, extra)  # Dataset order differs from frozen selected corpus.
    dataset.write_text(json.dumps(data), encoding='utf-8')
    result = json.loads((evaluation / 'result.json').read_text(encoding='utf-8'))
    result['dataset_sha256'] = fingerprint(data)
    (evaluation / 'result.json').write_text(json.dumps(result), encoding='utf-8')
    return evaluation, dataset, result


def test_selected_corpus_exports_and_blank_review_still_cannot_be_imported(review_case, tmp_path):
    evaluation, dataset, result = subset_case(review_case)
    result.update(status='scoring_failed', task_success_rate=None)
    (evaluation / 'result.json').write_text(json.dumps(result), encoding='utf-8')
    before = (evaluation / 'result.json').read_bytes()
    bundle = tmp_path / 'subset-review'
    export_review(evaluation, dataset, bundle)
    packet = json.loads((bundle / 'packet.json').read_text(encoding='utf-8'))['content']
    assert [t['task_id'] for t in packet['tasks']] == ['case']
    guide = (bundle / 'reading.md').read_text(encoding='utf-8')
    assert 'Document fact.' in guide and 'case:U1' in guide
    assert 'Unused document.' not in guide
    assert 'task_success_rate' not in guide and 'rubric_passed' not in guide
    with pytest.raises(ValidationError):
        import_review(evaluation, dataset, bundle, bundle / 'review.json', tmp_path / 'imported')
    assert (evaluation / 'result.json').read_bytes() == before


def test_full_corpus_uses_recorded_snapshot_order_even_if_taskset_order_differs(review_case):
    evaluation, dataset, result = subset_case(review_case)
    _, _, documents = load_taskset(dataset)
    result['corpus_hashes']['unused.md'] = documents['unused.md']['sha256']
    (evaluation / 'corpus/02-unused.md').write_bytes(b'Unused document.')
    (evaluation / 'result.json').write_text(json.dumps(result), encoding='utf-8')
    assert build_packet(evaluation, dataset)['tasks'][0]['task_id'] == 'case'


@pytest.mark.parametrize('mutation', ['missing_required', 'unknown', 'hash', 'snapshot'])
def test_selected_corpus_validation_is_not_weakened(review_case, tmp_path, mutation):
    evaluation, dataset, result = subset_case(review_case)
    if mutation == 'missing_required':
        result['corpus_hashes'] = {}
    elif mutation == 'unknown':
        result['corpus_hashes']['unknown.md'] = 'unknown'
    elif mutation == 'hash':
        result['corpus_hashes']['source.md'] = 'changed'
    else:
        (evaluation / 'corpus/01-source.md').write_text('Changed snapshot', encoding='utf-8')
    (evaluation / 'result.json').write_text(json.dumps(result), encoding='utf-8')
    with pytest.raises(VerificationError):
        export_review(evaluation, dataset, tmp_path / 'rejected')
    assert not (tmp_path / 'rejected').exists()


@pytest.fixture
def gap_evaluation(review_case):
    # Explicit synthetic labels: two supported lines and one uncited inference.
    from researchdesk.task_eval import quality_metrics
    from researchdesk.report_constraints import check_body
    from researchdesk.verification import load_run, split_units, prepare, assess
    evaluation, dataset, _ = review_case
    run = evaluation / 'tasks/case/research' / ('a' * 32)
    body, evidence, sources, _ = load_run(run)
    ledger = Ledger(run, resume=True)
    try:
        ledger.save_report(body + '\n' + body + '\nUncited inference.')
    finally:
        ledger.store.close()
    rows = split_units(load_run(run)[0])
    for row in rows:
        row.update(unit_id='case:' + row['unit_id'], task_id='case')
    payload, fingerprint = prepare(rows, evidence, sources)
    def labels(payload):
        return {'units': [dict(unit_id=r['unit_id'], factual=True, reason='Fixture reference',
            links=[dict(evidence_id=eid, verdict='supported', reason='Fixture reference') for eid in r['citations']])
            for r in payload['units']]}
    quality = assess(payload, labels)
    (evaluation / 'citation-quality.json').write_text(json.dumps(quality), encoding='utf-8')
    result = json.loads((evaluation / 'result.json').read_text(encoding='utf-8'))
    result['quality_input_sha256'] = fingerprint
    result['tasks'][0]['quality'] = quality_metrics(quality['units'])
    # This fixture intentionally creates a new frozen report and all its bindings.
    result['tasks'][0]['constraints'] = check_body(load_run(run)[0])
    (evaluation / 'result.json').write_text(json.dumps(result), encoding='utf-8')
    return evaluation, dataset, run


def test_quality_details_locate_gap_even_when_model_gate_passes(gap_evaluation, tmp_path, monkeypatch):
    import sys
    from researchdesk import task_eval
    evaluation, dataset, run = gap_evaluation
    before = {p: p.read_bytes() for p in (evaluation / 'result.json', evaluation / 'citation-quality.json',
                                         run / 'report.md', run / 'draft.md')}
    monkeypatch.setattr(task_eval, 'load_dotenv', lambda: pytest.fail('No credentials'))
    monkeypatch.setattr(task_eval, 'ModelJudge', lambda *a, **k: pytest.fail('No model'))
    monkeypatch.setattr(task_eval, 'TaskJudge', lambda *a, **k: pytest.fail('No model'))
    output = tmp_path / 'views'
    monkeypatch.setattr(sys, 'argv', ['task_eval', '--summarize', str(evaluation / 'result.json'),
        '--with-quality', '--dataset', str(dataset), '--output', str(output)])
    assert task_eval.main() == 0
    view = next(output.iterdir())
    summary = (view / 'summary.md').read_text(encoding='utf-8')
    details = (view / 'citation-quality.md').read_text(encoding='utf-8')
    assert '| valid | passed |' in summary and '(citation-quality.md)' in summary
    assert 'case:U3' in details and 'Uncited inference.' in details and 'Covered: False' in details
    assert 'Final acceptance: pending' in summary
    manifest = json.loads((view / 'provenance.json').read_text(encoding='utf-8'))
    assert manifest['api_requests'] == 0 and manifest['scoring_recomputed'] is False
    assert manifest['quality_sha256'] and manifest['input_sha256']
    assert all(p.read_bytes() == raw for p, raw in before.items())


@pytest.mark.parametrize('mutation', ['report', 'quality_text', 'quality_metrics', 'task_metrics', 'missing_quality', 'input_binding'])
def test_quality_view_rejects_changed_or_unbound_details(gap_evaluation, tmp_path, mutation):
    from researchdesk.task_eval import render_summary
    evaluation, dataset, run = gap_evaluation
    result_path, quality_path = evaluation / 'result.json', evaluation / 'citation-quality.json'
    if mutation == 'report':
        path = run / ('draft.md' if (run / 'draft.md').exists() else 'report.md')
        path.write_text(path.read_text(encoding='utf-8').replace('Uncited inference.', 'Changed fact.', 1), encoding='utf-8')
    elif mutation == 'missing_quality':
        quality_path.unlink()
    elif mutation in {'task_metrics', 'input_binding'}:
        result = json.loads(result_path.read_text(encoding='utf-8'))
        if mutation == 'task_metrics':
            result['tasks'][0]['quality']['evidence_coverage'] = 1
        else:
            result.pop('quality_input_sha256')
        result_path.write_text(json.dumps(result), encoding='utf-8')
    else:
        quality = json.loads(quality_path.read_text(encoding='utf-8'))
        if mutation == 'quality_text':
            quality['units'][0]['text'] = 'Unrelated body'
        else:
            quality['metrics']['evidence_coverage'] = 1
        quality_path.write_text(json.dumps(quality), encoding='utf-8')
    with pytest.raises((VerificationError, FileNotFoundError)):
        render_summary(result_path, tmp_path / 'view', with_quality=True, dataset=dataset)
    assert not (tmp_path / 'view').exists()

@pytest.mark.parametrize("mutation", ["result", "quality", "missing_quality"])
def test_import_rejects_model_files_changed_after_packet_validation(review_case, tmp_path, monkeypatch, mutation):
    evaluation, _, bundle = review_case
    review = filled_review(bundle)
    review_path = bundle / "filled-test-only.json"
    result_path = evaluation / "result.json"
    quality_path = evaluation / "citation-quality.json"
    original_read = Path.read_text
    model_result = json.loads(original_read(result_path, encoding="utf-8"))
    model_quality = json.loads(original_read(quality_path, encoding="utf-8"))

    def read_and_change_model(path, *args, **kwargs):
        text = original_read(path, *args, **kwargs)
        if path == review_path:
            if mutation == "result":
                model_result["task_success_rate"] = 0
                result_path.write_text(json.dumps(model_result), encoding="utf-8")
            elif mutation == "quality":
                model_quality["units"][0]["links"][0]["verdict"] = "partial"
                quality_path.write_text(json.dumps(model_quality), encoding="utf-8")
            else:
                quality_path.unlink()
        return text

    monkeypatch.setattr(Path, "read_text", read_and_change_model)
    output = tmp_path / "rejected"
    with pytest.raises(VerificationError, match="changed"):
        submit(review_case, review, output)
    assert not output.exists()


@pytest.mark.parametrize("add_quality_later", [False, True])
def test_import_missing_model_quality_is_bound_as_missing(review_case, tmp_path, monkeypatch, add_quality_later):
    evaluation, dataset, _ = review_case
    quality_path = evaluation / "citation-quality.json"
    original_quality = quality_path.read_text(encoding="utf-8")
    quality_path.unlink()
    result_path = evaluation / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result.update(status="scoring_failed", task_success_rate=None)
    result_path.write_text(json.dumps(result), encoding="utf-8")
    bundle = tmp_path / "missing-model-quality-bundle"
    export_review(evaluation, dataset, bundle)
    original_read = Path.read_text

    def read_and_add_quality(path, *args, **kwargs):
        text = original_read(path, *args, **kwargs)
        if add_quality_later and path == bundle / "filled-test-only.json":
            quality_path.write_text(original_quality, encoding="utf-8")
        return text

    monkeypatch.setattr(Path, "read_text", read_and_add_quality)
    output = tmp_path / "outcome"
    if add_quality_later:
        with pytest.raises(VerificationError, match="changed"):
            submit((evaluation, dataset, bundle), filled_review(bundle), output)
        assert not output.exists()
    else:
        outcome = submit((evaluation, dataset, bundle), filled_review(bundle), output)
        assert outcome["human_task_success_rate"] == 1
        assert outcome["model_task_success_rate"] is None
        assert outcome["disagreements"] == []
        assert not quality_path.exists()


@pytest.mark.parametrize('mutation', ['body', 'evidence', 'label_text', 'duplicate_label', 'missing_binding'])
def test_comparison_rejects_unbound_material(review_case, tmp_path, mutation):
    import sqlite3
    from researchdesk.evaluation_audit import compare_evaluations
    baseline, candidate = comparison_fixture(review_case, tmp_path)
    run = candidate / 'tasks/case/research' / ('a' * 32)
    quality_path = candidate / 'citation-quality.json'
    if mutation == 'body':
        (run / 'draft.md').write_text('Changed report.', encoding='utf-8')
    elif mutation == 'evidence':
        connection = sqlite3.connect(run / 'application.sqlite3')
        try:
            evidence = json.loads(connection.execute("SELECT payload FROM records WHERE kind='evidence'").fetchone()[0])
            evidence['snippet'] = 'Changed evidence.'
            connection.execute("UPDATE records SET payload=? WHERE kind='evidence'", (json.dumps(evidence),))
            connection.commit()
        finally:
            connection.close()
    elif mutation == 'missing_binding':
        path = candidate / 'result.json'
        result = json.loads(path.read_text(encoding='utf-8'))
        result.pop('quality_input_sha256')
        path.write_text(json.dumps(result), encoding='utf-8')
    else:
        quality = json.loads(quality_path.read_text(encoding='utf-8'))
        if mutation == 'label_text':
            quality['units'][0]['text'] = 'Unrelated report.'
        else:
            quality['units'].append(quality['units'][0])
        quality_path.write_text(json.dumps(quality), encoding='utf-8')
    with pytest.raises(VerificationError):
        compare_evaluations(baseline, candidate)


def test_comparison_missing_labels_are_unavailable_not_agreement(review_case, tmp_path):
    from researchdesk.evaluation_audit import compare_evaluations, audit_summary
    baseline, candidate = comparison_fixture(review_case, tmp_path)
    (candidate / 'citation-quality.json').unlink()
    audit = compare_evaluations(baseline, candidate)
    assert audit['citation_label_comparison_status'] == 'unavailable'
    assert audit['candidate_citation_quality_sha256'] is None
    assert audit['unchanged_report_citation_disagreements'] == []
    assert audit['unchanged_reports'] == 1
    assert 'unavailable' in audit_summary(audit)
