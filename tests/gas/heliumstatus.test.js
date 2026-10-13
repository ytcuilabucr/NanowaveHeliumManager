// Offline tests of apps_script/HeliumStatus.gs (run: node --test tests/gas).
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const {loadScript} = require('./fake_gas');

function setUp(opts) {
  const g = loadScript(opts);
  g.ctx.setup();
  g.token = g.props.TOKEN;
  return g;
}

// upload format: [Instr, pName, unit, value]
function upload(g, rows) {
  const p = {token: g.token};
  rows.forEach(([i, n, u, v], k) => Object.assign(p, {['Instr' + k]: i, ['pName' + k]: n, ['unit' + k]: u, ['pValue' + k]: v}));
  return g.post(p);
}

const SAMPLE = [
  ['Janis', 'Last Update Time', '', '2026/10/09 09:59:30'],
  ['Janis', 'LHe Level', 'in', '-2.090000'],
  ['Janis', 'LHe Volume', 'L', '0.000000'],
  ['ATL1', 'Diorde T', 'K', '4.175000'],
  ['ATL1', 'Liquid Vol', 'L', '65.020000'],
  ['ATL3', 'Liquid Vol', 'L', '41.887000'],
  ['MPHUB', 'Outlet Pressure', 'bar', '2.906000'],
  ['MPHUB', 'Enabled?', '', 'TRUE'],
  ['MPHUB', 'HiFlow Onset', 'Lpm', '100.000000'],
];

test('setup creates the Log header in production column order', () => {
  const g = setUp();
  const log = g.logBook().getSheetByName('Log');
  const header = log.getRange(1, 1, 1, log.getLastColumn()).getValues()[0];
  assert.equal(header.length, 68);
  assert.deepEqual(header.slice(0, 3), ['Log Time', 'Total Volume (L)', 'Janis_Last Update Time']);
  assert.equal(header[header.indexOf('MPHUB_Outlet Pressure (bar)') + 1], 'MPHUB_Equiv. Vol (L)');
  assert.ok(g.token);
});

test('rejects a wrong token', () => {
  const g = setUp();
  assert.deepEqual(g.post({token: 'nope', action: 'log'}), {ok: false, error: 'bad token'});
});

test('status upload writes one tab per instrument with Name (unit) headers', () => {
  const g = setUp();
  assert.deepEqual(upload(g, SAMPLE), {ok: true, values: SAMPLE.length});
  const mphub = g.status.getSheetByName('MPHUB');
  assert.deepEqual(mphub.getRange(1, 1, 2, 3).getValues(), [
    ['Outlet Pressure (bar)', 'Enabled?', 'HiFlow Onset (Lpm)'],
    [2.906, true, 100],
  ]);
  const janis = g.status.getSheetByName('Janis').getRange(2, 1).getValue();
  assert.ok(janis instanceof Date && janis.getHours() === 9 && janis.getMinutes() === 59);
});

test('a second upload updates values in place and appends new headers', () => {
  const g = setUp();
  upload(g, SAMPLE);
  upload(g, [['MPHUB', 'Outlet Pressure', 'bar', '3.000000'], ['MPHUB', 'User Data', '', '0']]);
  const m = g.status.getSheetByName('MPHUB');
  assert.deepEqual(m.getRange(1, 1, 2, 4).getValues(), [
    ['Outlet Pressure (bar)', 'Enabled?', 'HiFlow Onset (Lpm)', 'User Data'],
    [3, true, 100, 0],
  ]);
});

test('log row: Equiv. Vol and Total Volume as in production', () => {
  const g = setUp();
  upload(g, SAMPLE);
  const res = g.post({token: g.token, action: 'log'});
  assert.equal(res.ok, true);
  assert.equal(res.newColumns, 0);
  const dump = g.post({token: g.token, action: 'dump', rows: '1'});
  const row = dump.log[0];
  const equiv = (2.906 - 1) * 3000 / 757;
  assert.ok(Math.abs(row['MPHUB_Equiv. Vol (L)'] - equiv) < 1e-9);
  assert.ok(Math.abs(row['Total Volume (L)'] - (0 + 65.02 + 41.887 + equiv)) < 1e-9);
  assert.equal(row['ATL1_Diorde T (K)'], 4.175);
  assert.equal(row['MPHUB_Enabled?'], true);
  assert.equal(row['Log Time'], '2026/10/09 10:00:00');
});

test('production check: Equiv. Vol matches a real HeliumRecoveryStatusLog row', () => {
  const g = setUp();
  upload(g, [['MPHUB', 'Outlet Pressure', 'bar', '2.82946']]);
  g.post({token: g.token, action: 'log'});
  const row = g.post({token: g.token, action: 'dump', rows: '1'}).log[0];
  assert.ok(Math.abs(row['MPHUB_Equiv. Vol (L)'] - 7.250171731) < 1e-8);
});

test('log on empty status_now is refused', () => {
  const g = setUp();
  assert.deepEqual(g.post({token: g.token, action: 'log'}), {ok: false, error: 'status_now is empty'});
});

test('unknown tabs get extra Log columns; cleanup removes them and the row', () => {
  const g = setUp();
  upload(g, SAMPLE);
  upload(g, [['ZZTEST', 'Probe', 'V', '1.5']]);
  const res = g.post({token: g.token, action: 'log'});
  assert.equal(res.newColumns, 1);
  const log = g.logBook().getSheetByName('Log');
  assert.equal(log.getLastColumn(), 69);
  assert.deepEqual(g.post({token: g.token, action: 'cleanup', tab: 'ZZTEST', row: String(res.row)}),
    {ok: true, removedColumns: 1});
  assert.equal(log.getLastColumn(), 68);
  assert.equal(log.getLastRow(), 1);
  assert.equal(g.status.getSheetByName('ZZTEST'), null);
});

test('cleanup refuses non-test tabs', () => {
  const g = setUp();
  upload(g, SAMPLE);
  assert.equal(g.post({token: g.token, action: 'cleanup', tab: 'MPHUB'}).ok, false);
  assert.ok(g.status.getSheetByName('MPHUB'));
});

test('first log of a new month archives the old month and starts fresh', () => {
  const g = setUp({now: new Date('2026-10-31T23:58:00')});
  upload(g, SAMPLE);
  g.post({token: g.token, action: 'log'});
  g.setNow(new Date('2026-11-01T00:01:00'));
  g.post({token: g.token, action: 'log'});
  const archive = g.books['archive-HeliumRecoveryStatusLog_202610'];
  assert.ok(archive, Object.keys(g.books).join(','));
  assert.equal(archive.getSheetByName('Log').getLastRow(), 2);
  assert.deepEqual(g.moved, [{id: 'archive-HeliumRecoveryStatusLog_202610', folder: 'LogArchives'}]);
  const log = g.logBook().getSheetByName('Log');
  assert.equal(log.getLastRow(), 2); // header + the November row
});

test('archive name and folder come from the script properties', () => {
  const g = setUp({now: new Date('2026-10-31T23:58:00'), properties: {ARCHIVE_FOLDER_ID: 'arch', ARCHIVE_NAME: 'Lab'}});
  upload(g, SAMPLE);
  g.post({token: g.token, action: 'log'});
  g.setNow(new Date('2026-11-01T00:01:00'));
  g.post({token: g.token, action: 'log'});
  assert.deepEqual(g.moved, [{id: 'archive-Lab_202610', folder: 'folder arch'}]);
});

test('an archive that already exists gets the rows added, not a second file', () => {
  const g = setUp({now: new Date('2026-10-31T23:50:00')});
  upload(g, SAMPLE);
  g.post({token: g.token, action: 'log'});
  const existing = g.ctx.SpreadsheetApp.create('HeliumRecoveryStatusLog_202610');
  existing.folder = 'LogArchives';
  const prev = existing.insertSheet('Log');
  prev.getRange(1, 1, 2, 2).setValues([['Log Time', 'Total Volume (L)'], [new g.ctx.Date('2026-10-31T23:47:00'), 1]]);
  g.setNow(new Date('2026-10-31T23:58:00'));
  g.post({token: g.token, action: 'log'});
  g.setNow(new Date('2026-11-01T00:01:00'));
  g.post({token: g.token, action: 'log'});
  assert.equal(prev.getLastRow(), 4); // header + earlier row + the two October rows
  assert.deepEqual(g.moved, []);
  assert.equal(g.logBook().getSheetByName('Log').getLastRow(), 2);
});

test('setup refuses a script that is not attached to a spreadsheet', () => {
  const g = loadScript();
  g.ctx.SpreadsheetApp.getActive = () => null;
  assert.throws(() => g.ctx.setup(), /not attached to a spreadsheet/);
});

test('buildCharts makes one chart per spec with existing columns', () => {
  const g = setUp();
  upload(g, SAMPLE);
  g.post({token: g.token, action: 'log'});
  g.ctx.buildCharts();
  const charts = g.logBook().getSheetByName('Charts').getCharts();
  assert.equal(charts.length, 6);
  assert.equal(charts[1].options.title, 'ATL liquid volume');
  assert.equal(charts[1].ranges.length, 4); // time + ATL1..3
});

test('charts action rebuilds charts remotely and needs the token', () => {
  const g = setUp();
  upload(g, SAMPLE);
  g.post({token: g.token, action: 'log'});
  assert.deepEqual(g.post({token: g.token, action: 'charts'}), {ok: true, charts: 6});
  assert.deepEqual(g.post({token: g.token, action: 'charts'}), {ok: true, charts: 6}); // replaces, not duplicates
  assert.equal(g.logBook().getSheetByName('Charts').getCharts().length, 6);
  assert.equal(g.post({token: 'x', action: 'charts'}).ok, false);
});

test('readsheet returns header/value row of an allow-listed tab only', () => {
  const g = setUp();
  const src = g.ctx.SpreadsheetApp.openById('test-cryo-sheet');
  const cmi = src.insertSheet('CMI');
  cmi.getRange(1, 1, 2, 3).setValues([['Last Update Time', 'Level (inch)', 'Volume (L)'],
    [new g.ctx.Date('2026-10-09T09:17:40'), 8.3, 4.94608]]); // sandbox Date, as the script sees it
  const res = g.post({token: g.token, action: 'readsheet', id: src.getId(), gid: String(cmi.getSheetId())});
  assert.deepEqual(res, {ok: true, name: 'CMI',
    values: {'Last Update Time': '2026/10/09 09:17:40', 'Level (inch)': 8.3, 'Volume (L)': 4.94608}});
  assert.equal(g.post({token: g.token, action: 'readsheet', id: 'other', gid: '0'}).error, 'sheet not allowed');
  // CMI / Wei tabs kept in status_now itself are readable without READ_SOURCES
  const own = g.status.insertSheet('Wei Cryostat');
  own.getRange(1, 1, 2, 1).setValues([['He Level (L)'], [12.5]]);
  assert.deepEqual(g.post({token: g.token, action: 'readsheet', id: 'status-id', gid: String(own.getSheetId())}),
    {ok: true, name: 'Wei Cryostat', values: {'He Level (L)': 12.5}});
  assert.match(g.post({token: g.token, action: 'readsheet', id: src.getId(), gid: '1'}).error, /no tab/);
  assert.equal(g.post({token: 'bad', action: 'readsheet', id: src.getId(), gid: '0'}).ok, false);
});

const TRANSFER = {date: '2026-10-12', start: '13:05', end: '14:20', operator: 'Alice', atl: 'ATL3',
  atl_start: '87.5', atl_end: '52.0', cryostat: 'Janis', cryo_start: '-2.3', cryo_end: '18.4',
  cryo_unit: 'in', notes: 'test', source: 'Transfer Step 3'};

test('transfer creates the operation book in the status_log folder and appends rows', () => {
  const g = setUp();
  const res = g.post({token: g.token, action: 'transfer', ...TRANSFER});
  assert.equal(res.ok, true);
  assert.equal(res.sheet, 'Helium Recovery System Operation');
  assert.equal(res.tab, 'Helium Transfer');
  assert.equal(res.row, 2);
  assert.deepEqual(g.moved, [{id: res.id, folder: 'status_log folder'}]);
  assert.equal(g.props.OPERATION_SHEET_ID, res.id);
  const book = g.books[res.id];
  assert.equal(book.getSheetByName('Sheet1'), null);
  const [header, row] = book.getSheetByName('Helium Transfer').getRange(1, 1, 2, 14).getValues();
  assert.equal(header[0], 'Logged At');
  const rec = Object.fromEntries(header.map((h, i) => [h, row[i]]));
  assert.equal(rec['Operator'], 'Alice');
  assert.equal(rec['ATL Start Level (L)'], 87.5);
  assert.equal(rec['Cryostat End Level'], 18.4);
  assert.equal(rec['Start Time'], '13:05');
  // second transfer: same book, next row, no new file
  const again = g.post({token: g.token, action: 'transfer', ...TRANSFER, operator: 'Bob'});
  assert.equal(again.id, res.id);
  assert.equal(again.row, 3);
  assert.equal(g.moved.length, 1);
});

test('operators lists earlier operators, most recent first, without repeats', () => {
  const g = setUp();
  assert.deepEqual(g.post({token: g.token, action: 'operators'}).operators, []);
  ['Alice', 'Bob', 'Alice', ' Carol '].forEach(o => g.post({token: g.token, action: 'transfer', ...TRANSFER, operator: o}));
  assert.deepEqual(g.post({token: g.token, action: 'operators'}).operators, ['Carol', 'Alice', 'Bob']);
});

test('transfer test tabs: ZZTEST only, and only they can be cleaned up', () => {
  const g = setUp();
  const res = g.post({token: g.token, action: 'transfer', tab: 'ZZTEST1', ...TRANSFER});
  assert.equal(res.tab, 'ZZTEST1');
  assert.equal(g.post({token: g.token, action: 'transfer', tab: 'Other', ...TRANSFER}).tab, 'Helium Transfer');
  assert.equal(g.post({token: g.token, action: 'transfer_cleanup', tab: 'Helium Transfer'}).ok, false);
  assert.deepEqual(g.post({token: g.token, action: 'transfer_cleanup', tab: 'ZZTEST1'}), {ok: true, removed: true});
  assert.equal(g.books[res.id].getSheetByName('ZZTEST1'), null);
  assert.equal(g.post({token: 'bad', action: 'transfer', ...TRANSFER}).ok, false);
});

// --- Graphic View page -------------------------------------------------------------------

const SYSTEM = [
  ['_System', 'Last Update Time', '', '2026/10/09 09:59:40'],
  ['_System', 'Mode', '', 'transfer'],
  ['_System', 'Inactive', '', 'atl2,janis,wei'],
  ['_System', 'Alerts', '', '{"alerts":[["warning","outlet_low","pump","Outlet pressure low","outlet 2.25 bar",true]]}'],
];

test('_System tab is kept as text and never logged', () => {
  const g = setUp();
  upload(g, SAMPLE.concat(SYSTEM));
  const sys = g.status.getSheetByName('_System').getRange(1, 1, 2, 4).getValues();
  assert.equal(sys[1][1], 'transfer');
  assert.equal(sys[1][3], SYSTEM[3][3]); // JSON stays a string
  g.post({token: g.token, action: 'log'});
  const header = g.logBook().getSheetByName('Log').getRange(1, 1, 1, 200).getValues()[0];
  assert.ok(!header.some(h => String(h).indexOf('_System') === 0));
});

test('CMI / Wei tabs kept in status_now are not overwritten by the PC copy', () => {
  const g = setUp({properties: {READ_SOURCES: ''}});
  const cmi = g.status.insertSheet('CMI');
  cmi.getRange(1, 1, 2, 2).setValues([['Last Update Time', 'Level (inch)'], ['newer', 9.1]]);
  const res = upload(g, SAMPLE.concat([['CMI', 'Level', 'inch', '8.3'], ['Wei Cryostat', 'He Level', 'L', '5']]));
  assert.equal(res.ok, true);
  assert.deepEqual(cmi.getRange(2, 1, 1, 2).getValues()[0], ['newer', 9.1]);
  assert.equal(g.status.getSheetByName('Wei Cryostat'), null);
  assert.ok(g.status.getSheetByName('MPHUB'));
});

test('with another READ_SOURCES file, the CMI / Wei copy is still mirrored', () => {
  const g = setUp(); // READ_SOURCES = test-cryo-sheet
  upload(g, [['CMI', 'Level', 'inch', '8.3']]);
  assert.equal(g.status.getSheetByName('CMI').getRange(2, 1).getValue(), 8.3);
});

test('GET serves the embeddable Graphic View page; ?format=json the old reply', () => {
  const g = setUp();
  const page = g.get({});
  assert.equal(page.file, 'GraphicView');
  assert.equal(page.xframe, 'ALLOWALL');
  assert.deepEqual(g.get({format: 'json'}).json, {ok: true, sheet: 'status_now'});
});

test('VIEW_KEY protects the page and its data', () => {
  const g = setUp({properties: {VIEW_KEY: 'k1'}});
  assert.match(g.get({}).html, /Not authorised/);
  assert.equal(g.get({key: 'k1'}).viewKey, 'k1');
  assert.throws(() => g.ctx.graphicView('wrong'), /Not authorised/);
  assert.ok(g.ctx.graphicView('k1').tabs);
});

test('graphicView returns status tabs (times in ms) and the last 24 h of pressures', () => {
  const g = setUp({now: new Date('2026-10-09T10:00:00')});
  upload(g, SAMPLE.concat(SYSTEM, [['MPHUB', 'Inlet Pressure', 'bar', '1.25'], ['MPHUB', 'Tank Pressure', 'bar', '1.1'],
    ['MPHUB', 'Inlet Pressure Setpoint', 'mbar', '1270']]));
  g.post({token: g.token, action: 'log'});
  g.setNow(new Date('2026-10-09T10:03:00'));
  g.post({token: g.token, action: 'log'});
  const d = g.ctx.graphicView('');
  assert.equal(d.tabs.MPHUB['Outlet Pressure (bar)'], 2.906);
  assert.equal(typeof d.tabs.Janis['Last Update Time'], 'number');
  assert.equal(d.tabs._System.Mode, 'transfer');
  assert.equal(d.trend.t.length, 2);
  assert.deepEqual([...d.trend.inlet], [1.25, 1.25]);
  assert.deepEqual([...d.trend.setpoint], [1.27, 1.27]);
  assert.deepEqual([...d.trend.tank], [1.1, 1.1]);
  g.setNow(new Date('2026-10-10T10:02:00')); // a day later: only the newer row is within 24 h
  assert.equal(g.ctx.graphicView('').trend.t.length, 1);
});
