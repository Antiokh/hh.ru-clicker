"""No application startup: execute the actual activity renderer in an isolated DOM."""
from pathlib import Path
import shutil
import subprocess

import pytest


def run_activity(assertions, tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for UI helper checks")
    source = (Path(__file__).resolve().parents[1] / "static/js/app.js").read_text()
    helpers = source[source.index("function activityTimestamp("):
                     source.index("function hasUnknownApplication(")]
    pause = source[source.index("function hasUnknownApplication("):
                   source.index("function renderAccountVacancy(")]
    setup = r'''
const assert = require('node:assert/strict');
const fields = new Map();
const classes = new Set();
const region = {dataset:{},querySelector(selector) {
  if (!fields.has(selector)) fields.set(selector,{textContent:'',hidden:false});
  return fields.get(selector);
},classList:{toggle(key,on){if(on)classes.add(key);else classes.delete(key)}}};
const document = {getElementById:()=>region};
const field = name=>region.querySelector(`[data-activity="${name}"]`);
const timers=[];
const setInterval=(callback,ms)=>timers.push({callback,ms});
const t = key=>key;
const socket={readyState:1};
const State={ws:socket,snapshotSocket:socket,snapshotReceivedAt:1000,
  snapshotServerAt:Date.parse('2026-09-07T12:00:00Z'),lastSnapshot:{accounts:[],paused:false}};
const acc={idx:0,status:'collecting',activity:{phase:'collect',current:'Сбор вакансий',
  next:'Проверить новые вакансии',started_at:'2026-09-07T11:58:25Z',
  wait_until:'2026-09-07T12:01:00Z',progress:{done:2,total:5},requires_action:false}};
'''
    result = subprocess.run([node, "-e", setup + pause + helpers + assertions],
                            cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_valid_server_clock_elapsed_progress_and_known_countdown(tmp_path):
    run_activity(r'''
renderAccountActivity(acc,1000,Date.parse('2035-01-01T00:00:00Z'));
assert.match(field('time').textContent,/На этом этапе: 1 мин 35 с/);
assert.match(field('time').textContent,/Ожидание: ещё 1 мин 0 с/);
assert.equal(field('progress').textContent,'Прогресс этапа: 2 / 5');
assert.equal(field('current').textContent,'Сбор вакансий');
assert.equal(field('next').textContent,'Проверить новые вакансии');
assert(!classes.has('requires-action'));
assert(!classes.has('is-stale'));
assert.equal(field('action').hidden,true);
assert(!field('time').textContent.includes('завис'));
assert.equal(timers.length,1);
assert.equal(timers[0].ms,1000);
''', tmp_path)


def test_staleness_uses_receipt_clock_and_hides_timers(tmp_path):
    run_activity(r'''
renderAccountActivity(acc,16000,0);
assert.equal(classes.has('is-stale'),false); // exactly 15 seconds remains fresh
renderAccountActivity(acc,16001,0);
assert.equal(classes.has('is-stale'),true);
assert.equal(field('time').hidden,true);
assert.equal(field('time').textContent,'');
assert.equal(field('freshness').textContent,'Нет свежих данных: последнее известное состояние');
assert.equal(field('current').textContent,'Сбор вакансий');
assert.equal(field('next').textContent,'Проверить новые вакансии');
State.ws.readyState=3;
renderAccountActivity(acc,1000,0);
assert.equal(classes.has('is-stale'),true);
State.ws={readyState:1}; // reopen alone must not revive old snapshot
renderAccountActivity(acc,1000,0);
assert.equal(classes.has('is-stale'),true);
State.snapshotSocket=State.ws;
renderAccountActivity(acc,1000,0);
assert.equal(classes.has('is-stale'),false);
''', tmp_path)


def test_old_backend_fallback_has_no_invented_next_or_timers(tmp_path):
    run_activity(r'''
const view=accountActivityView({status:'checking',status_detail:'Проверяю лимит HH'},true,0);
assert.equal(view.current,'Проверяю лимит HH');
assert.equal(view.next,'Пока не указано сервером');
assert.equal(view.time,'');
assert.equal(view.progress,'');
const paused=accountActivityView({paused:true,paused_reason:'auto_errors',
  current_vacancy_title:'Old title',status_detail:'Proxy unavailable'},true,0);
assert.match(paused.current,/ошибки подряд/);
assert.match(paused.current,/Proxy unavailable/);
assert(!paused.current.includes('Old title'));
''', tmp_path)


def test_malformed_or_timezone_free_timers_and_progress_are_ignored(tmp_path):
    run_activity(r'''
for(const date of [null,0,true,'2026-09-07T12:00:00','bad','2026-99-99T12:00:00Z'])
  assert.equal(activityTimestamp(date),null);
assert.equal(activityTimestamp('2026-09-07T15:00:00+03:00'),State.snapshotServerAt);
for(const progress of [null,{done:true,total:3},{done:2,total:1},{done:0,total:0},
  {done:-1,total:3},{done:1.1,total:2},{done:1,total:Infinity}]) {
  const view=accountActivityView({...acc,activity:{...acc.activity,progress,
    started_at:'2026-09-07T12:00:00',wait_until:'tomorrow'}},true,State.snapshotServerAt);
  assert.equal(view.progress,'');
  assert.equal(view.time,'');
}
const future=accountActivityView({...acc,activity:{...acc.activity,
  started_at:'2026-09-07T13:00:00Z',wait_until:null}},true,State.snapshotServerAt);
assert.equal(future.time,'');
''', tmp_path)


def test_backend_strings_are_plain_text_and_expired_timer_not_claimed_complete(tmp_path):
    run_activity(r'''
acc.activity.current='<img src=x onerror=alert(1)>';
acc.activity.next='<script>private()</script>';
acc.activity.requires_action=true;
acc.activity.wait_until='2026-09-07T11:59:59Z';
renderAccountActivity(acc,1000,0);
assert.equal(field('current').textContent,acc.activity.current);
assert.equal(field('next').textContent,acc.activity.next);
assert.equal(field('current').innerHTML,undefined);
assert.match(field('time').textContent,/Плановое время наступило; ждём новый статус/);
assert.equal(field('action').textContent,'Требуется ваше действие');
assert.equal(classes.has('requires-action'),true);
''', tmp_path)


def test_local_tick_uses_no_transport(tmp_path):
    run_activity(r'''
let requests=0;
global.fetch=()=>{requests++;throw Error('must not call network')};
State.ws.send=()=>{requests++;throw Error('must not send commands')};
State.lastSnapshot.accounts=[acc];
State.snapshotReceivedAt=performance.now();
timers[0].callback();
assert.equal(requests,0);
assert.equal(field('current').textContent,'Сбор вакансий');
''', tmp_path)
