"""Reject non-count numeric values before the executor can admit any provider request."""
import pytest
from researchdesk.policy import ToolLimits
from test_policy import make_runtime


@pytest.mark.parametrize('field',['max_calls','max_provider_requests'])
def test_nan_cannot_be_a_count_budget(field):
    with pytest.raises(ValueError):
        ToolLimits(**{field:float('nan')})


@pytest.mark.parametrize('field,value',[
    ('max_calls',float('inf')), ('max_provider_requests',float('inf')),
    ('max_calls',0.5), ('max_provider_requests',1.0),
    ('max_calls',True), ('max_provider_requests',False),
    ('max_calls','2'), ('max_provider_requests',None), ('max_calls',-1),
    ('max_retries',0.5), ('max_retries',True), ('max_retries',float('nan')),
    ('max_retries',-1), ('max_retries',4),
    ('timeout_seconds',True), ('max_duration_seconds',False),
    ('timeout_seconds',float('nan')), ('max_duration_seconds',float('inf')),
    ('timeout_seconds',0), ('max_duration_seconds',-1),
    ('timeout_seconds','30'), ('max_duration_seconds',None),
    ('timeout_seconds',10**1000),
])
def test_invalid_limit_types_and_ranges_fail_at_configuration(field,value):
    with pytest.raises(ValueError): ToolLimits(**{field:value})


def test_zero_count_budgets_and_fractional_seconds_keep_their_meaning():
    limits=ToolLimits(max_calls=0,max_provider_requests=0,max_retries=0,
                      timeout_seconds=0.25,max_duration_seconds=2)
    assert limits.max_calls==limits.max_provider_requests==limits.max_retries==0
    assert limits.timeout_seconds==0.25 and limits.max_duration_seconds==2


def test_valid_limits_round_trip_without_coercion():
    from dataclasses import asdict
    limits=ToolLimits(max_calls=9,max_provider_requests=2,max_retries=3,
                      timeout_seconds=7,max_duration_seconds=40.5)
    import json
    restored=ToolLimits(**json.loads(json.dumps(asdict(limits))))
    assert asdict(restored)==asdict(limits)
    assert type(restored.max_calls) is int and type(restored.timeout_seconds) is int


@pytest.mark.parametrize('limits,code,calls',[
    (ToolLimits(max_calls=0),'call_budget_exceeded',0),
    (ToolLimits(max_provider_requests=0),'request_budget_exceeded',1),
])
def test_zero_budgets_block_new_work_without_contacting_provider(make_runtime,limits,code,calls):
    runtime,seen,_=make_runtime(limits=limits)
    try:
        result=runtime.executor.execute('search_sources',{'query':'fixture'})
        assert result['error_code']==code and seen==[]
        assert runtime.executor.calls==calls and runtime.executor.provider_requests==0
    finally:
        runtime.ledger.store.close()
