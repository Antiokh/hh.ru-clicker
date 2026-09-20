"""Execute the real pause/reconciliation UI helpers with an isolated Node DOM."""
from pathlib import Path
import shutil
import subprocess

import pytest


def run_ui(assertions, tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for UI helper checks")
    source = (Path(__file__).resolve().parents[1] / "static/js/app.js").read_text()
    helpers = source[source.index("function hasUnknownApplication("):
                     source.index("function updateCard(")]
    commands = source[source.index("function showCommandError("):
                      source.index("// ── Rendering")]
    escaping = source[source.index("function esc("):source.index("// Safe href:")]
    timestamp = source[source.index("function activityTimestamp("):
                       source.index("function activityDuration(")]
    setup = r'''
const assert = require('node:assert/strict');
const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {style:{}, textContent:'', title:'', disabled:false,
    setAttribute(){}});
  return elements.get(id);
}
const document = {getElementById:element};
const t = key => ({status_acc_paused:'Пауза аккаунта', status_all_paused:'Общая пауза',
  status_daily_limit:'Дневной лимит', status_hh_limit:'Лимит HH', card_waiting:'Ожидание',
  card_sending:'Отправка'})[key] || key;
const State = {ws:{readyState:1,send(){}},lastSnapshot:{accounts:[],config:{}},
  applicationChecks:new Map(),authChecks:new Map()};
const renderVacancySignals = () => '';
let fetch = async () => {throw Error('unexpected request')};
const pending = {vacancy_id:'12345',resume_id:'PRIVATE-RESUME',flow:'apply',
  recorded_at:'2026-09-07T00:00:00Z',reason_code:'transport_unknown'};
const account = () => ({idx:0,name:'Synthetic',resume_hash:'fixture-resume',mode:'oauth',paused:true,
  paused_reason:'outcome_unknown',pending_apply:pending,current_vacancy_title:'Old title'});
'''
    subprocess.run([node, "-e", setup + escaping + timestamp + helpers + commands +
                    "\n(async()=>{\n" + assertions + "\n})().catch(e=>{console.error(e);process.exitCode=1});"],
                   cwd=tmp_path, check=True, capture_output=True, text=True, timeout=10)


def test_pause_cause_precedes_old_vacancy_and_escapes_text(tmp_path):
    run_ui(r'''
const acc = account();
acc.status_detail = 'Proxy unavailable <img src=x onerror=boom>';
const info = accountPauseInfo(acc);
const html = renderAccountVacancy(acc, info);
assert.match(html, /Исход отклика неизвестен/);
assert.match(html, /Нужна сверка результата в HH/);
assert(html.indexOf('Proxy unavailable') < html.indexOf('Old title'));
assert.match(html, /&lt;img/);
assert(!html.includes('<img'));
assert(!html.includes('PRIVATE-RESUME'));
assert.equal(accountPauseInfo({paused:false}), null);
assert.match(renderAccountVacancy({status:'applying'}, null), /Отправка/);
assert.equal(hasUnknownApplication({pending_applies:[pending]}), true);
''', tmp_path)


def test_known_pause_reasons_and_global_pause_do_not_claim_manual_pause(tmp_path):
    run_ui(r'''
for (const [reason,expected] of [['auto_errors',/ошибки подряд/],['auth',/авторизация/],
                               ['limit',/Лимит HH/]]) {
  const info = accountPauseInfo({paused:true,paused_reason:reason,
    status_detail:'Пауза пользователем',current_vacancy_title:'Old'}, true);
  assert.match(info.label, expected);
  assert.match(info.detail, /Также включена общая пауза/);
  assert(!info.detail.includes('Пауза пользователем'));
}
assert.match(accountPauseInfo({paused:true,hard_stopped:true,hh_today_applies:200,
  hh_daily_limit:200}).label, /HH-лимит 200\/200/);
assert.match(accountPauseInfo({paused:true,hard_stopped:true,hh_today_applies:4,
  daily_limit:4,daily_sent:4}).label, /Дневной лимит 4\/4/);
assert.match(accountPauseInfo({paused:true,paused_reason:'manual',
  status_detail:'Пауза пользователем'}).detail, /Пауза пользователем/);
''', tmp_path)


def test_commands_disconnected_unknown_and_send_exception_never_retry(tmp_path):
    run_ui(r'''
let sent=[];
State.ws={readyState:3,send:obj=>sent.push(obj)};
assert.equal(sendCmd({type:'pause_toggle'}),false);
assert.match(element('dbg-err').textContent,/не отправлена и не поставлена в очередь/);
assert.equal(sent.length,0);
State.ws.readyState=1;
State.lastSnapshot.accounts=[account()];
assert.equal(sendCmd({type:'account_pause',idx:0}),false);
assert.match(element('dbg-err').textContent,/Исход отклика неизвестен/);
assert.equal(sent.length,0);
State.lastSnapshot.accounts[0].paused=false; // stopping is still permitted
assert.equal(sendCmd({type:'account_pause',idx:0}),true);
assert.deepEqual(sent.map(JSON.parse),[{type:'account_pause',idx:0}]);
State.ws.send=()=>{throw Error('synthetic transport exception')};
assert.equal(sendCmd({type:'pause_toggle'}),false);
assert.match(element('dbg-err').textContent,/не повторяется автоматически/);
assert.equal(sent.length,1);
''', tmp_path)


def test_reconciliation_exact_route_no_duplicate_and_late_identity_ignored(tmp_path):
    run_ui(r'''
State.lastSnapshot.accounts=[account()];
let calls=[],finish;
fetch=(path,options)=>{calls.push({path,options});return new Promise(r=>{finish=r})};
const task=reconcileApplication(0);
assert.equal(element('acc-application-check-btn-0').disabled,true);
await reconcileApplication(0);
assert.deepEqual(calls,[{path:'/api/account/0/reconcile-application',options:{method:'POST'}}]);
State.lastSnapshot.accounts=[{...account(),name:'Different account',resume_hash:'other'}];
renderApplicationCheck(State.lastSnapshot.accounts[0]);
finish({ok:true,json:async()=>({ok:true,confirmed:true,pending:false,paused:false})});
await task;
assert.equal(element('acc-application-check-result-0').textContent,'');
assert.equal(State.applicationChecks.has(0),false);
assert.equal(calls.length,1);
''', tmp_path)


@pytest.mark.parametrize("reply,expected", [
    ({"ok": True, "confirmed": True, "pending": True, "paused": True}, "Остались другие"),
    ({"ok": True, "confirmed": True, "pending": False, "paused": True}, "остаётся на паузе"),
    ({"ok": False, "message": "Отклик пока не подтверждён"}, "пока не подтверждён"),
    ({"ok": True}, "Проверка не подтвердила"),
    ({"ok": True, "confirmed": True}, "Проверка не подтвердила"),
])
def test_reconciliation_feedback_requires_confirmation(tmp_path, reply, expected):
    import json
    run_ui("""
State.lastSnapshot.accounts=[account()];
let count=0;
fetch=async()=>{count++;return {ok:true,json:async()=>(REPLY)}};
await reconcileApplication(0);
assert(element('acc-application-check-result-0').textContent.includes(EXPECTED));
assert.equal(State.lastSnapshot.accounts[0].paused,true);
assert.equal(count,1);
""".replace("REPLY", json.dumps(reply)).replace("EXPECTED", json.dumps(expected)), tmp_path)


def test_reconciliation_connection_failure_is_not_retried(tmp_path):
    run_ui(r'''
State.lastSnapshot.accounts=[account()];
let count=0;
fetch=async()=>{count++;throw Error('private transport detail must not leak')};
await reconcileApplication(0);
assert.equal(count,1);
assert.match(element('acc-application-check-result-0').textContent,/не повторяется автоматически/);
assert(!element('acc-application-check-result-0').textContent.includes('private transport'));
State.ws.readyState=3;
await reconcileApplication(0);
assert.equal(count,1);
''', tmp_path)


def test_scheduled_check_label_requires_matching_backend_schedule(tmp_path):
    run_ui(r'''
const acc=account();
acc.pending_apply={...pending,reconcile_attempts:0,reconcile_next_at:'2026-09-07T00:01:00Z'};
acc.activity={phase:'outcome_unknown',requires_action:false,wait_until:'2026-09-07T00:01:00Z'};
assert.equal(applicationCheckState(acc).scheduled,true);
renderApplicationCheck(acc);
assert.equal(element('acc-application-check-btn-0').textContent,'Проверить сейчас');
for(const attempts of [3,true,-1,0.5]) {
  acc.pending_apply.reconcile_attempts=attempts;
  assert.equal(applicationCheckState(acc).scheduled,false);
}
acc.pending_apply.reconcile_attempts=1;
acc.activity.requires_action=true;
assert.equal(applicationCheckState(acc).scheduled,false);
acc.activity.requires_action=false;
acc.activity.wait_until=null;
assert.equal(applicationCheckState(acc).scheduled,false);
''', tmp_path)


def test_new_automatic_attempt_or_removed_pending_clears_old_manual_feedback(tmp_path):
    run_ui(r'''
const acc=account(); State.lastSnapshot.accounts=[acc];
fetch=async()=>({ok:true,json:async()=>({ok:false,message:'Old manual failure'})});
await reconcileApplication(0);
renderApplicationCheck(acc);
assert.equal(element('acc-application-check-result-0').textContent,'Old manual failure');
acc.pending_apply.reconcile_last_started_at='2026-09-07T00:01:00Z';
renderApplicationCheck(acc);
assert.equal(element('acc-application-check-result-0').textContent,'');
assert.equal(State.applicationChecks.has(0),false);
await reconcileApplication(0);
acc.pending_apply=null; acc.paused_reason=''; acc.paused=false;
renderApplicationCheck(acc);
assert.equal(element('acc-application-check-result-0').textContent,'');
assert.equal(element('acc-application-check-0').style.display,'none');
''', tmp_path)


def test_own_inflight_check_keeps_busy_but_late_result_cannot_replace_new_auto_check(tmp_path):
    run_ui(r'''
const acc=account(); State.lastSnapshot.accounts=[acc];
let finish;
fetch=()=>new Promise(resolve=>{finish=resolve});
const task=reconcileApplication(0);
acc.pending_apply.reconcile_last_started_at='2026-09-07T00:01:00Z';
renderApplicationCheck(acc);
assert.equal(State.applicationChecks.get(0).busy,true);
assert.match(element('acc-application-check-result-0').textContent,/Сверяем/);
finish({ok:true,json:async()=>({ok:false,message:'Current manual failure'})});
await task;
renderApplicationCheck(acc);
assert.equal(element('acc-application-check-result-0').textContent,'');
assert.equal(State.applicationChecks.has(0),false);
''', tmp_path)


def test_active_server_check_prevents_additional_manual_request(tmp_path):
    run_ui(r'''
const acc=account(); acc.activity={phase:'receipt_check'};
State.lastSnapshot.accounts=[acc]; let count=0;
fetch=async()=>{count++;throw Error('must not start another check')};
renderApplicationCheck(acc);
assert.equal(element('acc-application-check-btn-0').disabled,true);
assert.match(element('acc-application-check-btn-0').textContent,/Проверяем/);
await reconcileApplication(0);
assert.equal(count,0);
''', tmp_path)
