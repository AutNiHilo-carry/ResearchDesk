"""Real process termination at publication boundaries; all opinions are synthetic."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import pytest
from researchdesk import stability_review
from test_judge_stability_inputs import completed_batch
from test_stability_review_import import fixture, snapshot


CHILD = '''import os,runpy,sys,threading
from pathlib import Path
evaluation,dataset,source,bundle,review,output,ready,boundary=sys.argv[1:]
def offline(event,args):
    if event in {'socket.connect','socket.getaddrinfo'}:
        raise RuntimeError('Network disabled in publication kill probe')
sys.addaudithook(offline)
import dotenv
def forbidden(*a,**k):
    raise RuntimeError('Credentials and models disabled in publication kill probe')
dotenv.load_dotenv=forbidden
import researchdesk.verification
researchdesk.verification.ModelJudge=forbidden
replace=os.replace
def pause_after_publish(source_path,target_path):
    replace(source_path,target_path)
    if Path(target_path)==Path(output)/boundary:
        Path(ready).write_bytes(b'ready')
        threading.Event().wait()
os.replace=pause_after_publish
sys.argv=['researchdesk.stability_review',evaluation,'--dataset',dataset,'--source',source,
          '--bundle',bundle,'--review',review,'--output',output]
runpy.run_module('researchdesk.stability_review',run_name='__main__')
'''


@pytest.mark.parametrize('boundary',['summary.md','result.json'])
def test_review_publication_kill_boundary(completed_batch,tmp_path,boundary):
    evaluation,dataset,source,bundle,review,_=fixture(completed_batch,tmp_path)
    before=snapshot(evaluation,source.parent,bundle)
    raw_review=review.read_bytes()
    output=tmp_path/'killed-record'
    ready=tmp_path/'kill-ready'
    environment=os.environ.copy()
    for key in ('PYTHONPATH','PYTHONHOME','DEEPSEEK_API_KEY','TAVILY_API_KEY'):
        environment.pop(key,None)
    environment['PYTHONUTF8']='1'
    command=[sys.executable,'-I','-c',CHILD,*map(str,(evaluation,dataset,source,bundle,review,output,ready)),boundary]
    process=subprocess.Popen(command,cwd=tmp_path,env=environment,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    try:
        deadline=time.monotonic()+30
        while not (ready.is_file() and ready.read_bytes()==b'ready'):
            if process.poll() is not None:
                stdout,stderr=process.communicate(timeout=5)
                pytest.fail(f'Probe exited before publication boundary: {stdout!r}; {stderr!r}')
            if time.monotonic()>deadline:
                pytest.fail('Publication boundary was not reached within 30 seconds')
            time.sleep(0.025)
        assert process.poll() is None
        process.kill()
        process.communicate(timeout=15)
        assert process.returncode != 0
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=15)
    published=boundary=='result.json'
    assert (output/'submitted-review.json').read_bytes()==raw_review
    assert (output/'summary.md').is_file()
    assert (output/'result.json').exists() is published
    if published:
        result=json.loads((output/'result.json').read_text(encoding='utf-8'))
        assert result['status']=='human_opinion_recorded' and result['completeness']=='complete'
        assert result['submitted_review_sha256']==hashlib.sha256(raw_review).hexdigest()
        assert result['accuracy'] is None and result['promotion']=='none' and result['final_acceptance']=='pending'
    retained=snapshot(output)
    with pytest.raises(ValueError):
        stability_review.record_review(evaluation,dataset,source,bundle,review,output)
    assert snapshot(output)==retained
    fresh=stability_review.record_review(evaluation,dataset,source,bundle,review,tmp_path/'fresh-record')
    assert fresh['exit_code']==0
    assert snapshot(evaluation,source.parent,bundle)==before and review.read_bytes()==raw_review
    receipt=dict(version='stability-review-publication-kill-v1',
        generation_contract=completed_batch[2]['generation_contract'],
        boundary='after_result_publish' if published else 'before_result_publish',
        process_killed=True, process_exit_code=process.returncode, canonical_result_present=published,
        attachments_present=True, source_files_unchanged=True, old_output_rejected=True,
        fresh_output_recorded=True, model_requests=0, search_requests=0,
        label_status='synthetic_not_independent_human', final_acceptance='pending')
    (tmp_path/'publication-receipt.json').write_text(json.dumps(receipt,indent=2),encoding='utf-8')
