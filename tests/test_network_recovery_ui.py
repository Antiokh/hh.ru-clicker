"""Network recovery UI: real JS, synthetic DOM, no application/account startup."""
import pytest

from tests.test_account_activity_ui import run_activity
from tests.test_pause_visibility_ui import run_ui


def test_network_proof_uses_existing_route_without_blind_resume(tmp_path):
    run_ui(r'''
const acc={...account(),paused_reason:'network_error',pending_apply:null,
  network_recovery:{reason:'connect_timeout',attempts:4,next_check_at:'2026-09-07T00:15:00Z',last_started_at:null}};
State.lastSnapshot.accounts=[acc];let calls=[],commands=[];State.ws.send=obj=>commands.push(obj);
renderAuthCheck(acc);
assert.equal(element('acc-auth-check-btn-0').textContent,'Проверить связь и продолжить');
assert.equal(sendCmd({type:'account_pause',idx:0}),false);
fetch=async(path,options)=>{calls.push({path,options});return {ok:true,json:async()=>({ok:true,verified:true,paused:false})}};
await recheckAccountAuth(0);
assert.deepEqual(calls,[{path:'/api/account/0/recheck-auth',options:{method:'POST'}}]);
assert.equal(acc.paused,true);
assert.equal(commands.length,0);
assert.match(element('acc-auth-check-result-0').textContent,/Связь и доступ к HH подтверждены/);
acc.paused=false;acc.paused_reason='';renderAuthCheck(acc);
assert.equal(element('acc-auth-check-0').hidden,true);
''', tmp_path)


def test_network_verification_cannot_override_other_holds_or_use_wrong_mode(tmp_path):
    run_ui(r'''
const acc={...account(),paused_reason:'network_error',pending_apply:null};State.lastSnapshot.accounts=[acc];
let calls=0;fetch=async()=>{calls++;throw Error('must not request')};
for(const mode of ['web','mobile',null,undefined]) {
  acc.mode=mode;acc.use_oauth=true;renderAuthCheck(acc);
  assert.equal(element('acc-auth-check-0').hidden,true);
  await recheckAccountAuth(0);assert.equal(sendCmd({type:'account_pause',idx:0}),false);
}
acc.mode='oauth';State.lastSnapshot.paused=true;await recheckAccountAuth(0);
State.lastSnapshot.paused=false;
for(const field of ['limit_exceeded','hard_stopped']){acc[field]=true;await recheckAccountAuth(0);acc[field]=false}
acc.pending_apply=pending;await recheckAccountAuth(0);acc.pending_apply=null;
acc.activity={phase:'auth_check'};renderAuthCheck(acc);
assert.equal(element('acc-auth-check-btn-0').disabled,true);
assert.equal(element('acc-auth-check-btn-0').textContent,'Проверяем связь с HH…');
await recheckAccountAuth(0);acc.activity=null;
State.ws.readyState=3;await recheckAccountAuth(0);
assert.equal(calls,0);
''', tmp_path)


@pytest.mark.parametrize("changed", ["last_started_at", "next_check_at"])
def test_old_manual_feedback_clears_after_auto_check_without_losing_own_busy(tmp_path, changed):
    run_ui(r'''
const acc={...account(),paused_reason:'network_error',pending_apply:null,
  network_recovery:{reason:'connect_timeout',next_check_at:'2026-09-07T00:15:00Z',last_started_at:null}};
State.lastSnapshot.accounts=[acc];let finish,calls=0;
fetch=()=>{calls++;return new Promise(resolve=>{finish=resolve})};
const task=recheckAccountAuth(0);
acc.network_recovery.CHANGED='2026-09-07T00:30:00Z';renderAuthCheck(acc);
assert.equal(element('acc-auth-check-btn-0').disabled,true);
await recheckAccountAuth(0);assert.equal(calls,1);
finish({ok:true,json:async()=>({ok:false,verified:false,paused:true,message:'Old manual feedback'})});
await task;
assert.equal(element('acc-auth-check-result-0').textContent,'');
assert.equal(State.authChecks.has(0),false);
'''.replace("CHANGED", changed), tmp_path)


def test_network_catch_does_not_leak_or_deny_server_schedule(tmp_path):
    run_ui(r'''
const acc={...account(),paused_reason:'network_error',pending_apply:null};State.lastSnapshot.accounts=[acc];
let calls=0;fetch=async()=>{calls++;throw Error('private proxy credential')};
await recheckAccountAuth(0);
const message=element('acc-auth-check-result-0').textContent;
assert.match(message,/расписание серверных проверок смотрите выше/);
assert(!message.includes('private'));assert(!message.includes('Автоматического повтора нет'));
assert.equal(calls,1);
assert(!accountPauseInfo(acc).label.includes('авторизация'));
assert.match(accountPauseInfo(acc).label,/связи/);
''', tmp_path)


def test_network_deadline_is_for_check_not_resume_and_not_exhausted_at_three(tmp_path):
    run_activity(r'''
acc.paused=true;acc.paused_reason='network_error';
acc.network_recovery={reason:'connect_timeout',attempts:9,last_error:'network',next_check_at:'2026-09-07T12:15:00Z'};
acc.activity={phase:'network_recovery',current:'Ждём восстановления сети',next:'Проверить связь без отправки откликов',
 started_at:null,wait_until:acc.network_recovery.next_check_at,requires_action:false};
renderAccountActivity(acc,1000,0);
assert.equal(field('time').textContent,'До проверки связи: 15 мин 0 с');
assert.match(field('network').textContent,/Попыток проверки связи: 9/);
assert(!field('network').textContent.includes('исчерпан'));
assert.equal(field('action').hidden,true);
renderAccountActivity(acc,16001,0);
assert.equal(field('time').hidden,true);
assert.match(field('freshness').textContent,/Нет свежих данных/);
assert.equal(timers.length,1); // existing local display tick only
''', tmp_path)


def test_network_metadata_is_allowlisted_and_absent_values_are_not_fake_zero(tmp_path):
    run_activity(r'''
acc.paused=true;acc.paused_reason='network_error';
for(const recovery of [null,[],true]){acc.network_recovery=recovery;assert.equal(networkRecoveryDetails(acc),'')}
for(const malformed of ['<img src=x onerror=boom>','toString',{toString:null}]) {
 acc.network_recovery={reason:malformed,last_error:malformed,attempts:true};
 const view=networkRecoveryDetails(acc);
 assert.equal(view,'Причина паузы: связь с HH не подтверждена');
 assert(!view.includes('0'));assert(!view.includes('img'));
}
for(const code of ['auth','challenge','rate_limit','unavailable','stale']) {
 acc.network_recovery={reason:'network_error',attempts:4,next_check_at:null,last_error:code};
 assert.match(networkRecoveryDetails(acc),/Автопроверки остановлены/);
}
acc.paused=false;assert.equal(networkRecoveryDetails(acc),'');
''', tmp_path)


def test_network_timer_requires_current_matching_schedule_and_respects_other_holds(tmp_path):
    run_activity(r'''
acc.paused=true;acc.paused_reason='network_error';
acc.network_recovery={reason:'timeout',attempts:2,last_error:'network',next_check_at:'2026-09-07T12:01:00Z'};
acc.activity={phase:'network_recovery',current:'Ждём связи',next:'Проверить связь',started_at:null,
 wait_until:'2026-09-07T12:01:00Z',requires_action:false};
assert.equal(accountActivityView(acc,true,State.snapshotServerAt,true).time,'');
acc.network_recovery.next_check_at=null;
assert.equal(accountActivityView(acc,true,State.snapshotServerAt).time,'');
acc.network_recovery.next_check_at=acc.activity.wait_until;
acc.network_recovery.last_error='challenge';
assert.equal(accountActivityView(acc,true,State.snapshotServerAt).time,'');
acc.network_recovery.last_error='network';acc.activity.requires_action=true;
assert.equal(accountActivityView(acc,true,State.snapshotServerAt).time,'');
acc.activity.requires_action=false;
const expired=accountActivityView(acc,true,State.snapshotServerAt+61000);
assert.equal(expired.time,'Время проверки наступило; ждём новый статус');
assert(!expired.time.includes('продолж'));
''', tmp_path)


def test_persistence_hold_overrides_even_retained_future_network_timestamps(tmp_path):
    run_activity(r'''
acc.paused=true;acc.paused_reason='network_error';
acc.network_recovery={reason:'timeout',attempts:1,last_error:'network',next_check_at:'2026-09-07T12:15:00Z'};
acc.activity={phase:'network_error',current:'Не удалось надёжно сохранить состояние',
 next:'Проверьте состояние панели',started_at:null,wait_until:'2026-09-07T12:15:00Z',requires_action:true};
renderAccountActivity(acc,1000,0);
assert.equal(field('time').hidden,true);
assert.equal(field('action').textContent,'Требуется ваше действие');
assert(!field('network').textContent.includes('будет выполнена'));
''', tmp_path)
