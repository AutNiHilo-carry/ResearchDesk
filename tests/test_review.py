import json
import shutil
from pathlib import Path
import pytest
from pydantic import ValidationError
from researchdesk.ledger import Ledger
from researchdesk.review import build_packet, export_review, import_review, fingerprint
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
