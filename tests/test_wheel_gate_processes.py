"""Source-environment process checks; CI additionally runs this matrix on a regular wheel install."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import pytest


def smoke_module():
    path=Path(__file__).parents[1]/'scripts/smoke_wheel.py'
    spec=importlib.util.spec_from_file_location('wheel_smoke_for_test',path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_acceptance_exit_codes_survive_real_process_boundaries(tmp_path,monkeypatch):
    module=smoke_module()
    import socket
    monkeypatch.setattr(socket,'create_connection',lambda *a,**k:pytest.fail('No parent network calls'))
    import dotenv
    monkeypatch.setattr(dotenv,'load_dotenv',lambda *a,**k:pytest.fail('No parent credential loading'))
    environment=os.environ.copy()
    for key in ('PYTHONPATH','PYTHONHOME','DEEPSEEK_API_KEY','TAVILY_API_KEY'):
        environment.pop(key,None)
    environment['PYTHONUTF8']='1'
    observed=[]
    def child(name,*args,expected=0):
        result=subprocess.run([sys.executable,'-I','-c',module.OFFLINE_GUARD,name,*map(str,args)],
                              cwd=tmp_path,env=environment,capture_output=True,text=True,timeout=90)
        assert result.returncode==expected,(name,result.returncode,result.stdout,result.stderr)
        observed.append((name,result.returncode))
    result=module.gate_smoke(tmp_path/'gate-matrix',child)
    assert result['execution_mode']=='injected_offline'
    assert result['source_files_unchanged'] and result['body_count']==213
    assert result['model_requests']==result['search_requests']==0
    assert [row['exit_code'] for row in result['cases']]==[0,3,0,3,1]
    assert [row['status'] for row in result['cases']]==['passed','failed','passed','unknown','input_rejected']
    assert observed[-1]==('researchdesk.report_constraints',3)
    assert len(observed)==6


@pytest.mark.parametrize('snippet',[
    "import socket; socket.getaddrinfo('example.invalid',443)",
    "import socket; socket.socket().connect(('127.0.0.1',9))",
])
def test_subprocess_guard_rejects_network_before_running_a_module(tmp_path,snippet):
    module=smoke_module()
    # Run the same audit-hook prefix, then an intentionally prohibited operation.
    prefix=module.OFFLINE_GUARD.split('module = sys.argv[1]')[0]
    result=subprocess.run([sys.executable,'-I','-c',prefix+snippet],cwd=tmp_path,
                          capture_output=True,text=True,timeout=15)
    assert result.returncode!=0 and 'Network disabled' in result.stderr


@pytest.mark.parametrize('target',['researchdesk.task_eval','researchdesk.evaluation_audit','researchdesk.judge_stability','researchdesk.stability_review'])
def test_gate_guard_refuses_credential_loading(tmp_path,target):
    module=smoke_module()
    prefix=module.OFFLINE_GUARD.split('sys.argv = [module]')[0]
    result=subprocess.run([sys.executable,'-I','-c',prefix+'\ndotenv.load_dotenv()',target],
                          cwd=tmp_path,capture_output=True,text=True,timeout=15)
    assert result.returncode!=0 and 'Credential loading disabled' in result.stderr


@pytest.mark.parametrize('target',['researchdesk.judge_stability','researchdesk.stability_review'])
def test_saved_stability_guard_refuses_model_construction(tmp_path,target):
    module=smoke_module()
    prefix=module.OFFLINE_GUARD.split('sys.argv = [module]')[0]
    result=subprocess.run([sys.executable,'-I','-c',prefix+'\nresearchdesk.verification.ModelJudge()',
                           target],cwd=tmp_path,capture_output=True,text=True,timeout=15)
    assert result.returncode!=0 and 'Model construction disabled' in result.stderr


def test_factuality_gate_exit_codes_survive_real_processes(tmp_path,monkeypatch):
    module=smoke_module()
    import socket
    monkeypatch.setattr(socket,'create_connection',lambda *a,**k:pytest.fail('No parent network calls'))
    import dotenv
    monkeypatch.setattr(dotenv,'load_dotenv',lambda *a,**k:pytest.fail('No parent credential loading'))
    environment=os.environ.copy()
    for key in ('PYTHONPATH','PYTHONHOME','DEEPSEEK_API_KEY','TAVILY_API_KEY'):
        environment.pop(key,None)
    environment['PYTHONUTF8']='1'
    observed=[]
    def child(name,*args,expected=0):
        process=subprocess.run([sys.executable,'-I','-c',module.OFFLINE_GUARD,name,*map(str,args)],
                               cwd=tmp_path,env=environment,capture_output=True,text=True,timeout=90)
        assert process.returncode==expected,(name,process.returncode,process.stdout,process.stderr)
        observed.append(process.returncode)
    result=module.factuality_gate_smoke(tmp_path/'factuality-matrix',child)
    assert observed==[0,3,3,0,3,3,1,0,3,1,1,0,3,0,1]
    assert [r['status'] for r in result['cases']]==['passed','review_required','unknown']
    assert result['fixture_model_rates']==dict(baseline=0,excluded=1)
    assert result['source_files_unchanged'] and result['model_requests']==result['search_requests']==0
    saved=result['saved_stability']
    assert [r['status'] for r in saved['cases']]==['passed','review_required','unknown','input_rejected']
    assert saved['source_files_unchanged'] and saved['model_requests']==saved['search_requests']==0
    assert [r['status'] for r in saved['opinion_import']['cases']]==['complete','partial','input_rejected','input_rejected']
    assert saved['opinion_import']['label_status']=='synthetic_not_independent_human'
    invalid=result['invalid_replay']
    assert [r['status'] for r in invalid['cases']]==['rendered','failed','passed','input_rejected']
    assert invalid['invalid_links']==1 and invalid['task_success_rate']==0
    assert invalid['source_files_unchanged'] and invalid['model_requests']==invalid['search_requests']==0
