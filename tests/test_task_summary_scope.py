import pytest

from researchdesk.task_eval import summary


@pytest.mark.parametrize('scope', [
    'local-documents-only; synthetic products; not a web-research benchmark',
    'local curated technical notes only; not live web research or a measured benchmark',
    None,
])
def test_summary_preserves_corpus_scope_and_candidate_label_status(scope):
    result = dict(status='complete', tasks=[], scope=scope, label_status='candidate; pending review')
    text = summary(result)
    assert f"Scope: {scope or 'unspecified'}" in text
    assert 'Label status: candidate; pending review' in text
    assert '受控虚构资料' not in text
    assert '需人工复核' in text
