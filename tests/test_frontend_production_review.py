"""Execute production JavaScript against hostile data and real Fetch headers."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "static/js/app.js").read_text(encoding="utf-8")
REACTION = (ROOT / "static/js/reaction-editor.js").read_text(encoding="utf-8")


def function(source, name, indent=2):
    start = source.index(" " * indent + "function " + name + "(")
    end = source.index("\n" + " " * indent + "}", start) + indent + 4
    return source[start:end]


def node(script):
    executable = shutil.which("node")
    if not executable:
        pytest.skip("Node.js required to execute browser JavaScript regressions")
    proc = subprocess.run([executable, "-e", script], text=True, capture_output=True, timeout=20)
    assert proc.returncode == 0, proc.stderr


DOM = r'''
const assert = require('node:assert/strict');
class Element {
  constructor() { this.children = []; this.style = {}; this.dataset = {}; this.value = ''; this.innerHTML = ''; this.classList = { add() {}, remove() {} }; }
  appendChild(el) { this.children.push(el); }
  replaceChildren() { this.children = []; this.innerHTML = ""; }
  addEventListener() {}
  querySelector() { return new Element(); }
  querySelectorAll() { return []; }
}
const elements = new Map();
const document = {
  getElementById(id) { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); },
  createElement() { return new Element(); },
  querySelectorAll() { return []; },
};
const payload = '\"><img src=x onerror=alert(1)> & </textarea><svg onload=alert(2)>';
'''


def test_csrf_fetch_is_same_origin_and_does_not_mutate_request_or_init():
    prefix = APP[: APP.index("  // Helper to determine if a job")]
    node(r'''
const assert = require('node:assert/strict');
const calls = [];
global.window = { location: { href: 'https://lab.example/studio', origin: 'https://lab.example' }, fetch: (resource, opts) => { calls.push({resource, opts}); return Promise.resolve(); } };
global.document = { querySelector: () => ({ content: 'session-secret' }) };
''' + prefix + r'''
})();
const originalHeaders = new Headers({'Content-Type': 'application/json'});
window.fetch('/api/jobs', {method: 'POST', headers: originalHeaders});
assert.equal(calls[0].opts.headers.get('X-CSRF-Token'), 'session-secret');
assert.equal(originalHeaders.has('X-CSRF-Token'), false);
const request = new Request('https://lab.example/api/jobs', {method:'DELETE', headers:{Authorization:'Bearer machine-token'}});
window.fetch(request);
assert.equal(calls[1].opts.headers.get('Authorization'), 'Bearer machine-token');
assert.equal(calls[1].opts.headers.get('X-CSRF-Token'), 'session-secret');
assert.equal(request.headers.has('X-CSRF-Token'), false);
window.fetch('https://other.example/api', {method:'POST'});
assert.equal(calls[2].opts.headers, undefined);
window.fetch('//other.example/api', {method:'DELETE'});
assert.equal(calls[3].opts.headers, undefined);
window.fetch('/api/jobs', {method:'GET'});
assert.equal(calls[4].opts.headers, undefined);
''')


def test_spectrum_layers_escape_uploaded_filenames_and_names():
    node(DOM + function(APP, "escapeHtml") + function(APP, "updateSpectrumLayersTray")
         + function(APP, "updateIRSpectrumLayersTray") + r'''
let loadedTheoreticalSpectra = [{visible:true, color:payload, name:payload, method:payload, transitions:[]}];
let loadedExperimentalSpectra = [{visible:true, color:payload, file_name:payload, sheet_name:payload, points_count:1, raw_hash:'123456789'}];
let loadedTheoreticalIRSpectra = [{visible:true, color:payload, name:payload, method:payload, modes:[]}];
let loadedExperimentalIRSpectra = [{visible:true, color:payload, file_name:payload, sheet_name:payload, label:payload, points_count:1}];
updateSpectrumLayersTray();
updateIRSpectrumLayersTray();
const cards = [...elements.values()].flatMap(el => el.children);
assert.equal(cards.length, 4);
for (const card of cards) {
  assert.equal(card.innerHTML.includes('<img'), false);
  assert.equal(card.innerHTML.includes('<svg'), false);
  assert.ok(card.innerHTML.includes('&lt;img'));
}
''')


def test_thermochemistry_species_preserve_pasted_text_without_html_execution():
    node(DOM + function(APP, "escapeHtml") + function(APP, "renderSpeciesCards", indent=4) + r'''
const state = {archiveEntries: []};
const loadJobs = () => [{jobId:payload, name:payload}];
const item = {id:'r1', coeff:1, name:payload, content:payload, sp_content:payload, primaryMode:'paste', spMode:'paste'};
const container = new Element();
renderSpeciesCards(container, [item], true);
const html = container.children[0].innerHTML;
assert.equal(html.includes('<img'), false);
assert.equal(html.includes('<svg'), false);
assert.ok(html.includes('&lt;/textarea&gt;'));
assert.equal((html.match(/<textarea/g) || []).length, 2);
''')


def test_xyz_fallback_and_nmr_metadata_are_escaped():
    node(DOM + function(APP, "escapeHtml") + function(APP, "init3DViewer")
         + function(APP, "normLevelStr") + function(APP, "updateNMRRefCustomVisibility") + r'''
const window = {};
let currentViewportBg,analysisStudio=null,viewer3D=null,isSpinning=false;
const MolecularControls={createSoftwareViewer(){throw Error(payload);}};
init3DViewer(payload, 1);
assert.equal(document.getElementById('engine-3d-viewport').innerHTML.includes('<img'), false);
let currentNMRNucleus = '1H';
let currentNMRData = {provenance: {method: payload, basis_set: payload}};
const NMR_PRESETS = {'1H': [{id:'standard', method:'B3LYP', basis:'def2-TZVP'}]};
document.getElementById('nmr-ref-preset').value = 'standard';
updateNMRRefCustomVisibility();
assert.equal(document.getElementById('nmr-ref-warning').innerHTML.includes('<img'), false);
assert.ok(document.getElementById('nmr-ref-warning').innerHTML.includes('&lt;img'));
''')


def test_reaction_thermochemistry_keeps_zero_and_null_values_distinct():
    node(DOM + function(REACTION, "renderThermoResults") + r'''
renderThermoResults({delta_E_elec_kcal_mol:0, delta_ZPE_kcal_mol:0, delta_H_kcal_mol:0, delta_G_kcal_mol:0, delta_S_j_mol_k:0, log10_k_eq:null});
for (const id of ['val-rxn-delta-e', 'val-rxn-delta-zpe', 'val-rxn-delta-h', 'val-rxn-delta-g']) assert.equal(document.getElementById(id).textContent, '0.00 kcal/mol');
assert.equal(document.getElementById('val-rxn-delta-s').textContent, '0.00');
assert.equal(document.getElementById('val-rxn-k-eq').textContent, '-');
renderThermoResults({delta_G_hartree:0, log10_k_eq:0});
assert.equal(document.getElementById('val-rxn-delta-g').textContent, '0.000000 Eh');
assert.equal(document.getElementById('val-rxn-k-eq').textContent, 'log10 K = 0.00');
''')


def test_forget_credentials_reports_failure_without_clearing_local_state():
    source = (ROOT / "static/js/kaggle_credentials.js").read_text(encoding="utf-8")
    start = source.index('    forget.addEventListener("click", async () => {')
    end = source.index('\n    });', start) + len('\n    });')
    node(DOM + r'''
const status = new Element(), forget = new Element();
let click;
forget.addEventListener = (_event, handler) => { click = handler; };
const username = {value:'researcher'}, key = {value:'api-token'}, remember = {checked:true};
const localStorage = {removeItem() { throw new Error('local state must not be cleared after a failed deletion'); }};
const fetch = async () => ({ok:false, status:503, json: async () => ({error:'Vault unavailable'})});
const render = async () => {};
const USER_KEY='username', KEY_KEY='key';
''' + source[start:end] + r'''
(async () => {
  await click();
  assert.equal(forget.disabled, false);
  assert.ok(status.textContent.includes('Could not delete saved credentials'));
  assert.equal(username.value, 'researcher');
  assert.equal(key.value, 'api-token');
  assert.equal(remember.checked, true);
})();
''')


def test_kaggle_sync_applies_remote_stop_to_existing_job_and_active_count():
    node(function(APP, "kaggleRemotePatch") + function(APP, "isActiveOnKaggle")
         + function(APP, "mergeRemoteJobs") + r'''
const assert = require('node:assert/strict');
const TERMINAL_STATUSES = ['complete', 'error', 'cancelled'];
let jobs = [{jobId:'chem-tools-test', kaggleUsername:'user', status:'running', submittedAt:1}];
const loadJobs = () => jobs;
const saveJobs = value => { jobs = value; };
const loadRemovedIds = () => [];
const isRemovedId = () => false;
const renderJobs = () => {};
for (const status of ['cancelled', 'error']) {
  jobs[0].status = 'running';
  mergeRemoteJobs('user', '', [{job_id:'chem-tools-test', owner:'user', status,
    remote_state:status.toUpperCase(), remote_active:false, cf_sync_status:'SYNCED'}]);
  assert.equal(jobs[0].status, status);
  assert.equal(jobs.filter(isActiveOnKaggle).length, 0);
  assert.equal(jobs[0].cfSyncStatus, 'SYNCED');
}
mergeRemoteJobs('user', '', [{job_id:'chem-tools-running', status:'running', remote_active:true}]);
assert.equal(jobs.filter(isActiveOnKaggle).length, 1);
const running = jobs.find(j => j.jobId === 'chem-tools-running');
mergeRemoteJobs('user', '', [{job_id:running.jobId, status:'error', remote_status_unknown:true}]);
assert.equal(running.status, 'running');
assert.equal(isActiveOnKaggle(running), false);
assert.equal(isActiveOnKaggle({status:'running', backend:'server_local'}), false);
assert.equal(isActiveOnKaggle({status:'running', backend:'local_agent'}), false);
assert.equal(isActiveOnKaggle({status:'restarting', remoteActive:false}), false);
assert.equal(isActiveOnKaggle({status:'queued', remoteActive:true}), true);
''')


def test_queue_reserves_remote_queued_and_uncertain_slots_without_counting_local_jobs():
    node(function(APP, "reservesKaggleSlot") + r'''
const assert = require('node:assert/strict');
const TERMINAL_STATUSES = ['complete', 'error', 'cancelled'];
assert.equal(reservesKaggleSlot({jobId:'chem-tools-queued', status:'queued', remoteActive:true}), true);
assert.equal(reservesKaggleSlot({jobId:'job_pending', status:'queued'}), false);
assert.equal(reservesKaggleSlot({jobId:'chem-tools-old', status:'running', remoteStatusUnknown:true}), true);
assert.equal(reservesKaggleSlot({status:'restarting', remoteActive:false}), false);
assert.equal(reservesKaggleSlot({status:'cancelled', remoteActive:true}), false);
assert.equal(reservesKaggleSlot({status:'running', backend:'server_local'}), false);
''')
