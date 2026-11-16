import json
from pathlib import Path
import sys
import pytest
from researchdesk.agent import PROMPT
from researchdesk.ledger import Ledger
from researchdesk.repair import REPAIR_PROMPT, fork_run, repair_run
from researchdesk.report_policy import bound_prompt, policy_spec, prompt_text
from researchdesk.storage import RecoveryError, atomic_text
from researchdesk.verification import digest


def set_policy(directory, version):
    path = directory / "run.json"
    meta = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    meta["report_policy"] = policy_spec(version)
    atomic_text(path, json.dumps(meta, ensure_ascii=False))


def test_shipped_v1_generation_and_repair_prompts_remain_frozen():
    assert digest(PROMPT) == "ea443d90d4ef51f9b1b8e4aa0f0c5940ed229fb9c3ac758b27bc9504ecf2fa56"
    assert digest(REPAIR_PROMPT) == "ca55287815439140b2779f43212032495d3813d7af2b90652331a04558872cf4"
    assert prompt_text(PROMPT, "generation", policy_spec("v1")) == PROMPT
    assert prompt_text(REPAIR_PROMPT, "repair", policy_spec("v1")) == REPAIR_PROMPT


def test_policy_and_actual_prompt_are_pinned_and_cannot_be_swapped_on_resume(tmp_path):
    ledger = Ledger(tmp_path / "run")
    try:
        set_policy(ledger.directory, "v2")
        effective = bound_prompt(ledger, PROMPT, "generation")
        assert effective != PROMPT
        assert bound_prompt(ledger, PROMPT, "generation") == effective
        with pytest.raises(RecoveryError, match="Effective report prompt changed"):
            bound_prompt(ledger, PROMPT + "changed", "generation")
        set_policy(ledger.directory, "v1")
        with pytest.raises(RecoveryError, match="pinned run policy"):
            bound_prompt(ledger, PROMPT, "generation")
        atomic_text(ledger.directory / "run.json", "{}")
        with pytest.raises(RecoveryError, match="cannot be removed"):
            bound_prompt(ledger, PROMPT, "generation")
    finally:
        ledger.store.close()


def test_legacy_history_keeps_v1_and_cannot_be_relabelled_as_a_new_policy(tmp_path):
    ledger = Ledger(tmp_path / "run")
    try:
        ledger.store.add_event(dict(event="model_started", model_call_id="legacy"))
        assert bound_prompt(ledger, PROMPT, "generation") == PROMPT
        assert ledger.store.records("report_policy") == []
        set_policy(ledger.directory, "v2")
        with pytest.raises(RecoveryError, match="after model calls"):
            bound_prompt(ledger, PROMPT, "generation")
    finally:
        ledger.store.close()


def test_repair_fork_inherits_version_and_checks_prompt_binding_before_any_request(tmp_path):
    source = tmp_path / "source"
    ledger = Ledger(source)
    meta = dict(format_version=2, status="completed", mode="live", model="fixture", report_policy=policy_spec("v2"))
    atomic_text(source / "run.json", json.dumps(meta))
    atomic_text(source / "input.json", json.dumps({"messages": [{"role": "user", "content": "Fixture"}]}))
    bound_prompt(ledger, PROMPT, "generation")
    saved = ledger.add_source("Fixture", "fixture://source", "A fact.", "local")
    evidence = ledger.add_evidence("A fact", saved["id"], "A fact.")
    ledger.save_report(f"A fact.[{evidence['id']}]")
    ledger.store.close()
    candidate = tmp_path / "candidate"
    fork_run(source, candidate)
    state = json.loads((candidate / "repair-state.json").read_text(encoding="utf-8"))
    assert state["repair_prompt_sha256"] == digest(prompt_text(REPAIR_PROMPT, "repair", policy_spec("v2")))
    set_policy(candidate, "v1")
    class Never:
        def __call__(self, payload):
            raise AssertionError("Policy conflict must not invoke the verifier")
    with pytest.raises(RecoveryError, match="Saved repair prompt"):
        repair_run(None, candidate, checker=Never(), resume=True)


def test_resume_flags_cannot_override_report_version(monkeypatch):
    from researchdesk.cli import main
    monkeypatch.setattr(sys, "argv", ["researchdesk", "--resume", "a"*32, "--report-policy", "v2"])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2


def test_unknown_or_forged_instruction_hash_is_rejected():
    forged = dict(policy_spec("v2"), generation_sha256="forged")
    with pytest.raises(RecoveryError):
        prompt_text(PROMPT, "generation", forged)
    with pytest.raises(RecoveryError):
        policy_spec("v3")


def test_agent_receives_versioned_prompt_and_preserves_local_scope(tmp_path, monkeypatch):
    from researchdesk import agent
    from types import SimpleNamespace
    captured = {}
    monkeypatch.setattr(agent, "ChatDeepSeek", lambda **kwargs: SimpleNamespace(callbacks=[]))
    def capture(**kwargs):
        captured.update(kwargs)
        return "offline-agent"
    monkeypatch.setattr(agent, "create_deep_agent", capture)
    ledger = Ledger(tmp_path / "run")
    try:
        set_policy(ledger.directory, "v2")
        assert agent.build_agent(ledger, allowed_tools=["read_source", "record_evidence"]) == "offline-agent"
        prompt = captured["system_prompt"]
        assert "禁止外部检索" in prompt
        binding = ledger.store.records("report_prompt")[0]
        assert binding["prompt_sha256"] == digest(prompt)
        assert prompt.endswith(prompt_text("", "generation", policy_spec("v2")))
    finally:
        ledger.store.close()


def test_workflow_forwards_candidate_policy_without_rewriting_input(tmp_path):
    from researchdesk.workflow import create_workflow, run_workflow
    doc = tmp_path / "doc.md"
    doc.write_text("Frozen fixture.", encoding="utf-8")
    root = tmp_path / "workflow"
    create_workflow(root, "Original question", [doc], model="fixture", local_only=True, report_policy="v2")
    calls = []
    def stop_before_request(args, **kwargs):
        calls.append(args)
        assert args[args.index("--report-policy") + 1] == "v2"
        assert args[args.index("--") + 1] == "Original question"
        frozen = Path(args[args.index("--document") + 1])
        assert frozen.read_bytes() == doc.read_bytes()
        raise KeyboardInterrupt()
    state = run_workflow(root, research_runner=stop_before_request)
    assert state["status"] == "interrupted" and len(calls) == 1
    assert state["plan"]["report_policy_version"] == "v2"
