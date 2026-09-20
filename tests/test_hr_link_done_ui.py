"""Synthetic browser-storage regressions; no production data or network."""
import pathlib
import subprocess


def test_hr_link_done_persistence():
    source = pathlib.Path('static/js/app.js').read_text()
    section = source[source.index("const _LLM_DONE_KEY"):source.index('function _llmRenderHrLinks')]
    script = r'''
const assert = require('node:assert/strict');
let stored = '{}', fail = false, alerts = 0, handler;
const localStorage = {
  getItem: () => stored,
  setItem: (key, value) => { if (fail) throw Error('quota'); stored = value; }
};
const window = {addEventListener: (name, fn) => { handler = fn; }};
const alert = () => { alerts++; };
const _llmRowsCache = [];
function _llmRenderHrLinks() {}
''' + section + r'''
_llmToggleLinkDone('1', 'https://example.org/?a=1&b=2');
assert.ok(JSON.parse(stored)['1|https://example.org/?a=1&b=2']);
assert.deepEqual(_llmReadLinkDone(), _llmLinksDone);
// A stale tab must preserve unrelated updates already written by another tab.
stored = JSON.stringify({...JSON.parse(stored), 'other|url': 'done'});
_llmToggleLinkDone('2', 'https://example.org/2');
assert.equal(JSON.parse(stored)['other|url'], 'done');
fail = true;
const before = JSON.stringify(_llmLinksDone);
_llmToggleLinkDone('2', 'https://example.org/2');
assert.equal(JSON.stringify(_llmLinksDone), before);
assert.equal(alerts, 1);
fail = false;
_llmToggleLinkDone('2', 'https://example.org/2');
assert.equal(JSON.parse(stored)['2|https://example.org/2'], undefined);
stored = '{}'; handler({key: _LLM_DONE_KEY});
assert.deepEqual(_llmLinksDone, {});
stored = '[]';
_llmToggleLinkDone('3', 'https://example.org/3');
assert.equal(stored, '[]');
assert.equal(alerts, 2);
'''
    subprocess.run(['node', '-e', script], check=True, capture_output=True, text=True)
