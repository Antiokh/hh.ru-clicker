"""Mocked UI logic for explicit auth proof, using the real JS functions."""
import json

import pytest

from tests.test_pause_visibility_ui import run_ui


def test_auth_verification_exact_route_and_no_blind_resume(tmp_path):
    run_ui(r'''
const acc={...account(),paused_reason:'auth',pending_apply:null,cookies_expired:false};
State.lastSnapshot.accounts=[acc];let commands=[];State.ws.send=obj=>commands.push(obj);
assert.equal(sendCmd({type:'account_pause',idx:0}),false);
assert.equal(commands.length,0);
let calls=[];
fetch=async(path,options)=>{calls.push({path,options});return {ok:true,json:async()=>({ok:true,verified:true,paused:false})}};
await recheckAccountAuth(0);
assert.deepEqual(calls,[{path:'/api/account/0/recheck-auth',options:{method:'POST'}}]);
assert.equal(acc.paused,true); // never optimistically resume
assert.match(element('acc-auth-check-result-0').textContent,/Вход в HH подтверждён/);
acc.paused=false;acc.paused_reason='';renderAuthCheck(acc);
assert.equal(element('acc-auth-check-0').hidden,true);
assert.equal(State.authChecks.has(0),false);
''', tmp_path)


def test_auth_inflight_double_click_and_old_account_response_are_safe(tmp_path):
    run_ui(r'''
const acc={...account(),paused_reason:'auth',pending_apply:null};
State.lastSnapshot.accounts=[acc];let finish,calls=0;
fetch=()=>{calls++;return new Promise(resolve=>{finish=resolve})};
const request=recheckAccountAuth(0);
assert.equal(element('acc-auth-check-btn-0').disabled,true);
await recheckAccountAuth(0);assert.equal(calls,1);
State.lastSnapshot.accounts=[{...acc,resume_hash:'different',name:'Other fixture'}];
renderAuthCheck(State.lastSnapshot.accounts[0]);
finish({ok:true,json:async()=>({ok:true,verified:true,paused:false})});await request;
assert.equal(element('acc-auth-check-result-0').textContent,'');
assert.equal(State.authChecks.has(0),false);
''', tmp_path)


def test_auth_request_guard_disconnected_server_busy_and_protected_other_state(tmp_path):
    run_ui(r'''
const acc={...account(),paused_reason:'auth',pending_apply:null};
State.lastSnapshot.accounts=[acc];let calls=0;
fetch=async()=>{calls++;throw Error('must not call')};
State.ws.readyState=3;await recheckAccountAuth(0);assert.equal(calls,0);
assert.match(element('dbg-err').textContent,/Нет связи/);
State.ws.readyState=1;acc.activity={phase:'auth_check'};
renderAuthCheck(acc);assert.equal(element('acc-auth-check-btn-0').disabled,true);
await recheckAccountAuth(0);assert.equal(calls,0);
acc.activity=null;acc.pending_apply=pending;
await recheckAccountAuth(0);assert.equal(calls,0);
for(const reason of ['manual','limit','auto_errors','challenge','hh_rate_limit']){
 acc.pending_apply=null;acc.paused_reason=reason;renderAuthCheck(acc);
 assert.equal(element('acc-auth-check-0').hidden,true);
 await recheckAccountAuth(0);assert.equal(calls,0);
}
''', tmp_path)


@pytest.mark.parametrize("reply", [
    {"ok": True}, {"ok": True, "verified": "yes", "paused": False},
    {"ok": True, "message": "Вход в HH подтверждён"},
    {"ok": True, "verified": True, "paused": True},
    {"ok": False, "verified": False, "paused": True, "reason": "network", "message": "Не удалось связаться с HH"},
    {"ok": False, "busy": True, "message": "Проверка уже выполняется"},
])
def test_auth_errors_and_incomplete_success_never_claim_verification(tmp_path, reply):
    run_ui(r'''
const acc={...account(),paused_reason:'auth',pending_apply:null};State.lastSnapshot.accounts=[acc];
fetch=async()=>({ok:true,json:async()=>(REPLY)});
await recheckAccountAuth(0);
assert.equal(acc.paused,true);
assert(!element('acc-auth-check-result-0').textContent.includes('Вход в HH подтверждён'));
assert(element('acc-auth-check-result-0').textContent.length>0);
'''.replace("REPLY", json.dumps(reply)), tmp_path)


def test_auth_transport_error_is_safe_and_never_retried(tmp_path):
    run_ui(r'''
const acc={...account(),paused_reason:'auth',pending_apply:null};State.lastSnapshot.accounts=[acc];
let calls=0;fetch=async()=>{calls++;throw Error('private proxy credential must not leak')};
await recheckAccountAuth(0);
assert.equal(calls,1);
assert.match(element('acc-auth-check-result-0').textContent,/Автоматического повтора нет/);
assert(!element('acc-auth-check-result-0').textContent.includes('private proxy'));
''', tmp_path)


def test_challenge_and_rate_limit_labels_do_not_misdiagnose_auth_or_quota(tmp_path):
    run_ui(r'''
const rate=accountPauseInfo({paused:true,paused_reason:'hh_rate_limit'});
assert.match(rate.label,/частоту запросов/);
assert(!rate.label.includes('HH-лимит'));
assert(!rate.label.includes('авторизация'));
const challenge=accountPauseInfo({paused:true,paused_reason:'challenge'});
assert.match(challenge.label,/проверку доступа/);
assert(!challenge.label.includes('авторизация'));
''', tmp_path)


def test_auth_verifier_requires_native_oauth_mode_and_no_global_or_limit_hold(tmp_path):
    run_ui(r'''
const acc={...account(),paused_reason:'auth',pending_apply:null};State.lastSnapshot.accounts=[acc];
let calls=0;fetch=async()=>{calls++;throw Error('no requests expected')};
for(const mode of ['web','mobile','',null,undefined]) {
  acc.mode=mode;acc.use_oauth=true;renderAuthCheck(acc);
  assert.equal(element('acc-auth-check-0').hidden,true);
  await recheckAccountAuth(0);assert.equal(calls,0);
  assert.equal(sendCmd({type:'account_pause',idx:0}),false);
  assert.match(element('dbg-err').textContent,/Восстановите вход/);
}
acc.mode='oauth';State.lastSnapshot.paused=true;
renderAuthCheck(acc);assert.equal(element('acc-auth-check-btn-0').disabled,true);
assert.match(element('acc-auth-check-result-0').textContent,/общая пауза/);
await recheckAccountAuth(0);assert.equal(calls,0);
State.lastSnapshot.paused=false;
for(const key of ['hard_stopped','limit_exceeded']){
 acc[key]=true;renderAuthCheck(acc);assert.equal(element('acc-auth-check-btn-0').disabled,true);
 assert.match(element('acc-auth-check-result-0').textContent,/ограничение откликов/);
 await recheckAccountAuth(0);assert.equal(calls,0);acc[key]=false;
}
''', tmp_path)
