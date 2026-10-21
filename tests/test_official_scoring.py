import json
import pytest
from src.benchmarks.excytin_bench.official_scoring import SDKEvaluator
from scripts.score_ltm_wm_batch import metrics

Q={'context':'context', 'question':'target?', 'answer':'final', 'solution':['first','second','third','final']}


def test_reflection_controls_answer_and_stops_steps():
    calls=[]
    responses=iter(['Analysis: no\nIs_Answer_Correct: False', 'Analysis: corrected\nIs_Answer_Correct: True'])
    def request(messages,json_mode):
        calls.append((messages,json_mode));return next(responses)
    r=SDKEvaluator(request).checking(Q,'submitted')
    assert r['reward']==1 and len(calls)==2
    assert 'second reviewer' in calls[1][0][0]['content']
    assert 'context target?' in calls[0][0][1]['content']
    assert all(not c[1] for c in calls)


def test_official_partial_reward_uses_reflection_skips_final():
    initial={str(i):{'analysis':'x','is_step_correct':'False'} for i in range(4)}
    reflected={str(i):{'analysis':'x','is_step_correct':'True'} for i in range(4)}
    responses=iter(['Is_Answer_Correct: False','Is_Answer_Correct: False',json.dumps(initial),json.dumps(reflected)])
    calls=[]
    def request(messages,json_mode):calls.append((messages,json_mode));return next(responses)
    r=SDKEvaluator(request).checking(Q,'intermediate only')
    assert r['reward']==pytest.approx(0.4+0.16+0.064)
    assert [c[1] for c in calls]==[False,False,True,True]
    assert 'Submitted Answer:' in calls[2][0][1]['content']
    assert 'intermediate only' in calls[2][0][1]['content']


def test_answer_format_failure_is_not_zero_reward():
    with pytest.raises(ValueError):SDKEvaluator(lambda *args:'invalid').checking(Q,'x')


def test_incomplete_metrics_do_not_treat_judge_failure_as_wrong():
    r={'a':{'status':'scored','reward':1,'success':True},'b':{'status':'judge_failed','reward':None}}
    m=metrics(r,3)
    assert m['acc'] is None and m['mean_reward'] is None
    assert m['acc_scored']==1 and m['judge_failed']==1 and m['pending']==1
    r['b']={'status':'scored','reward':0.4,'success':False}
    m=metrics(r,2)
    assert m['acc']==0.5 and m['mean_reward']==0.7
