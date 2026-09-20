"""Execute the cycle report's real renderer without app startup or transport."""
from pathlib import Path
import shutil
import subprocess

import pytest


def run_cycle(assertions, tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for UI helper checks")
    source = (Path(__file__).resolve().parents[1] / "static/js/app.js").read_text()
    timestamp = source[source.index("function activityTimestamp("):
                       source.index("function activityDuration(")]
    functions = source[source.index("function cycleReportView("):
                       source.index("function accountActivityView(")]
    setup = r'''
const assert=require('node:assert/strict');
const nodes=new Map();
const region={querySelector(selector){
  if(!nodes.has(selector))nodes.set(selector,{textContent:'',hidden:false,open:false});
  return nodes.get(selector);
}};
const document={getElementById:()=>region};
const field=name=>region.querySelector(`[data-cycle="${name}"]`);
const report={cycle_id:'fixture-cycle',started_at:'2026-09-07T12:00:00Z',finished_at:null,
 status:'running',found_raw:10,found_unique:8,considered:8,processed:7,sent:2,already:2,
 skipped:3,skip_reasons:[{key:'already',label:'Уже откликались',count:2},
 {key:'blacklist',label:'Чёрный список',count:1}],errors:1,unknown:1,
 questionnaires_pending:1,remaining:1,partial:false,operation_errors:0};
'''
    result = subprocess.run([node, "-e", setup + timestamp + functions + assertions],
                            cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_confirmed_counts_are_not_inferred_from_daily_session_or_unknown(tmp_path):
    run_cycle(r'''
renderCycleReport({idx:0,daily_sent:84,sent:0,cycle_report:report});
assert.equal(field('title').textContent,'За текущий цикл');
assert.equal(field('found').textContent,'8');
assert.equal(field('sent').textContent,'2');
assert.equal(field('skipped').textContent,'3');
assert.equal(field('remaining').textContent,'1');
assert.match(field('processed').textContent,/Разобрано: 7/);
assert.match(field('facts').textContent,/Уже откликались: 2 \(входит в «Пропущено»\)/);
assert.match(field('flags').textContent,/Не подтверждено: 1 \(не считаются отправленными\)/);
''', tmp_path)


def test_unavailable_old_backend_and_invalid_counts_never_become_zero(tmp_path):
    run_cycle(r'''
for(const value of [undefined,null,{},[],{...report,cycle_id:''},
  {...report,started_at:'2026-09-07T12:00:00'}])assert.equal(cycleReportView(value).available,false);
renderCycleReport({idx:0});
assert.equal(field('body').hidden,true);
assert.equal(field('empty').hidden,false);
for(const value of [null,undefined,true,'0',-1,Infinity,1.5]){
 const view=cycleReportView({...report,found_unique:value,sent:value,skipped:value,remaining:value});
 assert.equal(view.found,'—');assert.equal(view.sent,'—');
 assert.equal(view.skipped,'—');assert.equal(view.remaining,'—');
}
const zero=cycleReportView({...report,found_unique:0,sent:0,skipped:0,remaining:0});
assert.equal(zero.found,'0');assert.equal(zero.sent,'0');
''', tmp_path)


def test_partial_collection_and_unresolved_work_remain_explicit(tmp_path):
    run_cycle(r'''
const view=cycleReportView({...report,partial:true,found_raw:null,remaining:null,operation_errors:2});
assert.match(view.notice,/неполный итог/);
assert.equal(view.remaining,'—');
assert.match(view.facts,/Найдено до общего удаления дублей: —/);
assert.match(view.flags,/Ошибки операций: 2/);
assert.match(view.flags,/Анкеты ждут ответа: 1/);
assert.match(view.flags,/Не подтверждено: 1/);
assert.equal(view.sent,'2');
''', tmp_path)


def test_finished_cycle_is_not_labeled_current_and_details_survive_ticks(tmp_path):
    run_cycle(r'''
const acc={idx:0,cycle_report:{...report,finished_at:'2026-09-07T12:03:00Z',status:'blocked'}};
renderCycleReport(acc);
assert.equal(field('title').textContent,'За последний цикл');
assert.equal(field('status').textContent,'Приостановлен');
field('details').open=true;
renderCycleReport(acc);
assert.equal(field('details').open,true);
acc.cycle_report={...report,cycle_id:'new-cycle'};
renderCycleReport(acc);
assert.equal(field('details').open,false);
assert.equal(field('title').textContent,'За текущий цикл');
acc.cycle_report=null;renderCycleReport(acc);
assert.equal(field('body').hidden,true);
assert.equal(field('found').textContent,'');
''', tmp_path)


def test_reasons_are_bounded_plain_text_and_invalid_counts_are_ignored(tmp_path):
    run_cycle(r'''
const reasons=[{key:'untrusted',label:'<img src=x onerror=boom>',count:2},
 {key:'string',label:'Invalid count',count:'8'}, {key:'negative',label:'Negative',count:-1},
 {key:'zero',label:'Zero',count:0},{key:'boolean',label:'Boolean',count:true}];
renderCycleReport({idx:0,cycle_report:{...report,skip_reasons:reasons}});
assert.equal(field('reasons').textContent,'<img src=x onerror=boom>: 2');
assert.equal(field('reasons').innerHTML,undefined);
assert.match(field('preview').textContent,/<img/);
assert(!field('reasons').textContent.includes('Invalid count'));
''', tmp_path)
