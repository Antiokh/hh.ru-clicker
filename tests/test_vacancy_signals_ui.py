"""Run the real small UI renderers in Node without loading the application."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_signals_render_unknown_zero_expiry_and_untrusted_values():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for UI renderer checks")
    source = (Path(__file__).resolve().parents[1] / "static/js/app.js").read_text()
    render = source[source.index("function renderVacancySignals("):
                    source.index("function expireVacancyOnlineSignals(")]
    expire = source[source.index("function expireVacancyOnlineSignals("):
                    source.index("setInterval(expireVacancyOnlineSignals,")]
    assertions = r'''
const assert = require('node:assert/strict');
assert.equal(renderVacancySignals({}, false, 0), '');
assert.match(renderVacancySignals({}, true, 0), /нет доступных сигналов/);
assert.match(renderVacancySignals({skills_match_percent: 0}), /Навыки 0%/);
assert.equal(renderVacancySignals({skills_match_percent: true}), '');
assert.equal(renderVacancySignals({skills_match_percent: NaN}), '');
assert.equal(renderVacancySignals({skills_match_percent: 101}), '');
assert.equal(renderVacancySignals({relations: ['<script>', '__proto__']}), '');
assert.match(renderVacancySignals({relations:['got_response']}), /Уже есть отклик/);
const future = {manager_activity: {is_online_until: '2099-01-01T00:00:00Z'}};
const deadline = Date.parse(future.manager_activity.is_online_until);
assert.match(renderVacancySignals(future, false, deadline - 1), /Менеджер онлайн/);
assert.equal(renderVacancySignals(future, false, deadline), '');
assert.equal(renderVacancySignals({manager_activity:{last_activity_at:'2099-01-01T00:00:00Z'}}), '');
let removed = false;
const element = {dataset:{hhOnlineUntil:'0'},style:{},removeAttribute:()=>{removed=true;}};
global.document = {querySelectorAll:()=>[element]};
expireVacancyOnlineSignals();
assert.equal(removed, true);
assert.match(element.textContent, /устарел/);
'''
    subprocess.run([node, "-e", render + expire + assertions], check=True,
                   capture_output=True, text=True, timeout=10)
