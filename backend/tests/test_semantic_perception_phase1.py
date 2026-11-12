import pytest
from app.contracts import BrowserObservation, PrivacyViolation
from app.privacy import classify_and_redact
from app.observation import match_semantic_target


def observation(name='Find products', context='Nearby $129', x=10):
    return BrowserObservation.model_validate({
        'tabId':1,'url':'https://example.org','title':'Shop',
        'scroll':{'y':0,'viewport':{'w':800,'h':600}},
        'interactive':[{'targetId':'search','role':'searchbox','name':name,'tag':'input',
            'visible':True,'enabled':True,'semantic':{'elementId':'search','role':'searchbox',
            'semanticName':name,'controlType':'search_input','contextText':context,
            'interactability':{'bounds':{'x':x,'y':30,'width':200,'height':40},
                'visible':True,'opacity':1,'pointerEvents':'auto','occluded':False}}}]})

def test_semantic_is_redacted_and_available_to_planner():
    obs=observation(context='token=sk-live-abc123')
    payload,_=classify_and_redact(obs,instruction='find products')
    ctx=payload.to_context_block()
    assert 'control=search_input' in ctx
    assert 'bounds=10.0,30.0' in ctx
    assert 'sk-live-abc123' not in ctx
    assert match_semantic_target(payload.interactive,name='Find products').target_id=='search'

def test_sensitive_field_context_not_forwarded():
    obs=observation(name='Password',context='private nearby')
    obs.interactive[0].input_type='password'
    payload,_=classify_and_redact(obs,instruction='login')
    assert payload.interactive[0].semantic.context_text==''

def test_malformed_bounds_fail_closed():
    obs=observation()
    obs.interactive[0].semantic.interactability['bounds']['x']=float('nan')
    with pytest.raises(PrivacyViolation):classify_and_redact(obs,instruction='search')

def test_occluded_target_is_not_reused():
    obs=observation()
    obs.interactive[0].semantic.interactability['occluded']=True
    assert match_semantic_target(obs.interactive,target_id='search',name='Find products') is None
