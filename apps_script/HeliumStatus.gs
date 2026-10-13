/**
 * Helium Recovery status + log — Apps Script bound to the `status_now` spreadsheet.
 *
 * Same layout as the production sheets in Shared drives/Helium Recovery/Data_logs:
 *   status_now  (like CurrentData): one tab per instrument (Janis, ATL1-3, MPHUB, ...),
 *               row 1 = "Name (unit)" headers, row 2 = latest values.
 *               The CMI / Wei Cryostat tabs are written here by their own systems and are
 *               only read by this script.
 *   status_log  (like HeliumRecoveryStatusLog): tab "Log", one row per snapshot:
 *               Log Time | Total Volume (L) | <Tab>_<Header> ... (incl. MPHUB_Equiv. Vol (L)),
 *               archived each month to "<ARCHIVE_NAME>_YYYYMM" (e.g. HeliumRecoveryStatusLog_202011)
 *               in the LogArchives folder.
 *
 * The lab PC POSTs the upload format as query parameters:
 *   Instr0, pName0, pValue0, unit0, Instr1, ... (+ token) -> update status_now tabs
 *   action=log (+ token)                                   -> append one Log row (every 3 min)
 * Tab "_System" (operation mode, switched-off instruments, alerts) comes with the status
 * upload; tabs starting with "_" are not logged.
 *
 * GET on the web app URL shows the Graphic View page (GraphicView.html), e.g. embedded in
 * Notion; ?format=json returns {ok, sheet} instead.
 *
 * Install once: paste this file (Extensions > Apps Script), add the script properties
 * below (Project Settings > Script properties), run `setup` (copy the logged token),
 * then Deploy > Web app (execute as Me, access Anyone).
 */

// Per-site settings, kept in the script properties (not in this file):
//   LOG_SHEET_ID       id of the log spreadsheet (unset = log into this file)
//   ARCHIVE_FOLDER_ID  id of the monthly archive folder (unset = "LogArchives" next to the log file)
//   ARCHIVE_NAME       archive file name before "_YYYYMM" (unset = HeliumRecoveryStatusLog)
//   READ_SOURCES       comma-separated ids of other spreadsheets the lab PC may read
//                      (this file is always readable, for CMI / Wei-He3 tabs kept here)
//   VIEW_KEY           optional: the Graphic View page then needs ?key=<VIEW_KEY>
const PROPS = PropertiesService.getScriptProperties();
const LOG_SHEET_ID = PROPS.getProperty('LOG_SHEET_ID') || '';
const ARCHIVE_FOLDER_ID = PROPS.getProperty('ARCHIVE_FOLDER_ID') || '';
const ARCHIVE_NAME = PROPS.getProperty('ARCHIVE_NAME') || 'HeliumRecoveryStatusLog';
const ARCHIVE_FOLDER = 'LogArchives';
const LOG_TAB = 'Log';
const CHART_TAB = 'Charts';
const TAB_ORDER = ['Janis', 'CMI', 'Wei Cryostat', 'ATL1', 'ATL2', 'ATL3', 'MPHUB'];
const TANK_L_PER_BAR = 3000 / 757; // MPHUB gas tank -> liquid-equivalent litres per bar above 1 bar

// Column order of the production HeliumRecoveryStatusLog "Log" tab.
const ATL_COLS = ['Last Update Time', 'Diorde T (K)', 'Input Gas Flow (Lpm)', 'Liquid Vol (L)', 'LiQ Rate (L/Day)',
  'Status Machine', 'Liquid Mode', 'Transfer Mode', 'Status LEDs', 'Dewar Capacity (L)', 'Liquid Volume (L)',
  'Heater Power (%)', 'Dewar Pres (psi)'];
const LOG_HEADER = ['Log Time', 'Total Volume (L)']
  .concat(['Last Update Time', 'LHe Level (in)', 'LHe Volume (L)'].map(h => 'Janis_' + h))
  .concat(['Last Update Time', 'Level (inch)', 'Volume (L)'].map(h => 'CMI_' + h))
  .concat(['Last Update Time', 'He Level (L)'].map(h => 'Wei Cryostat_' + h))
  .concat(ATL_COLS.map(h => 'ATL1_' + h), ATL_COLS.map(h => 'ATL2_' + h), ATL_COLS.map(h => 'ATL3_' + h))
  .concat(['Last Update Time', 'Inlet Pressure (bar)', 'Tank Pressure (bar)', 'Outlet Pressure (bar)', 'Equiv. Vol (L)',
    'Enabled?', 'Outlet Tank Level (%)', 'Oil Temperature (deg C)', 'Helium Flow In (Lpm)', 'User Data',
    'Inlet Pressure Setpoint (mbar)', 'Comp on Setpoint (mbar)', 'Comp off Setpoint (mbar)',
    'Max Outlet Pressure (mbar)', 'Pressure Setpoint (mbar)', 'HiFlow Onset (Lpm)', 'BP Valve P Gain',
    'BP Valve I Gain', 'BP Valve D Gain'].map(h => 'MPHUB_' + h));
// Liquid-volume columns summed into "Total Volume (L)" (as in production).
const VOLUME_COLS = ['Janis_LHe Volume (L)', 'CMI_Volume (L)', 'Wei Cryostat_He Level (L)', 'ATL1_Liquid Vol (L)',
  'ATL2_Liquid Vol (L)', 'ATL3_Liquid Vol (L)', 'MPHUB_Equiv. Vol (L)'];

function setup() {
  if (!SpreadsheetApp.getActive()) {
    throw new Error('This script is not attached to a spreadsheet. Open status_now > Extensions > Apps Script ' +
      'and put the code there.');
  }
  const log = logBook_();
  const sh = log.getSheetByName(LOG_TAB) || log.insertSheet(LOG_TAB);
  if (sh.getLastColumn() === 0) sh.getRange(1, 1, 1, LOG_HEADER.length).setValues([LOG_HEADER]).setFontWeight('bold');
  sh.setFrozenRows(1);
  sh.setFrozenColumns(2);
  if (!log.getSheetByName(CHART_TAB)) log.insertSheet(CHART_TAB);
  [SpreadsheetApp.getActive(), log].forEach(ss => {
    const first = ss.getSheetByName('Sheet1');
    if (first && first.getLastRow() === 0 && ss.getSheets().length > 1) ss.deleteSheet(first);
  });
  const props = PropertiesService.getScriptProperties();
  if (!props.getProperty('TOKEN')) props.setProperty('TOKEN', Utilities.getUuid());
  Logger.log('status sheet: ' + SpreadsheetApp.getActive().getName());
  Logger.log('log sheet:    ' + log.getName() + (LOG_SHEET_ID ? '' : '  (LOG_SHEET_ID not set: logging into this file)'));
  Logger.log('archives:     ' + archiveFolder_(log).getName() + '/' + ARCHIVE_NAME + '_YYYYMM');
  Logger.log('HR_CRYO_SHEET_ID=' + SpreadsheetApp.getActive().getId());
  Logger.log('HR_STATUS_SHEET_TOKEN=' + props.getProperty('TOKEN'));
}

function doPost(e) {
  const p = e.parameter;
  if (p.token !== PropertiesService.getScriptProperties().getProperty('TOKEN')) return json_({ok: false, error: 'bad token'});
  const lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    if (p.action === 'log') return json_(appendLog_());
    if (p.action === 'dump') return json_(dump_(Number(p.rows || 3)));
    if (p.action === 'cleanup') return json_(cleanup_(p.tab, Number(p.row || 0)));
    if (p.action === 'charts') return json_({ok: true, charts: buildCharts()});
    if (p.action === 'readsheet') return json_(readSheet_(p.id, p.gid));
    if (p.action === 'transfer') return json_(logTransfer_(p));
    if (p.action === 'operators') return json_(operators_(p.tab));
    if (p.action === 'transfer_cleanup') return json_(transferCleanup_(p.tab));
    return json_(writeStatus_(p));
  } finally {
    lock.releaseLock();
  }
}

function doGet(e) {
  const p = (e && e.parameter) || {};
  if (p.format === 'json') return json_({ok: true, sheet: SpreadsheetApp.getActive().getName()});
  const key = PROPS.getProperty('VIEW_KEY');
  if (key && p.key !== key) return HtmlService.createHtmlOutput('<p style="font-family:sans-serif">Not authorised.</p>');
  const page = HtmlService.createTemplateFromFile('GraphicView');
  page.viewKey = p.key || '';
  return page.evaluate()
    .setTitle('Helium Recovery · Graphic View')
    .addMetaTag('viewport', 'width=device-width, initial-scale=1')
    .setXFrameOptionsMode(HtmlService.XFrameOptionsMode.ALLOWALL); // embeddable (Notion)
}

// Tabs the PC writes. The CMI / Wei Cryostat tabs belong to their own systems: when they are
// kept in this file (no READ_SOURCES), the PC's copy of them is not written back, so a newer
// reading is never overwritten by an older one.
const OWN_TABS = ['Janis', 'ATL1', 'ATL2', 'ATL3', 'MPHUB', '_System'];

/** Upload format -> one tab per instrument, header row + value row. */
function writeStatus_(p) {
  const byTab = {};
  for (let i = 0; p['Instr' + i] !== undefined; i++) {
    const unit = p['unit' + i];
    const header = unit ? p['pName' + i] + ' (' + unit + ')' : p['pName' + i];
    (byTab[p['Instr' + i]] = byTab[p['Instr' + i]] || []).push([header, parse_(p['pValue' + i])]);
  }
  const ss = SpreadsheetApp.getActive();
  let n = 0;
  Object.keys(byTab).forEach(tab => {
    if (!READ_SOURCES.length && OWN_TABS.indexOf(tab) < 0 && !/^ZZTEST/.test(tab)) return;
    const sh = ss.getSheetByName(tab) || ss.insertSheet(tab);
    const width = Math.max(sh.getLastColumn(), 1);
    const header = sh.getRange(1, 1, 1, width).getValues()[0].filter(String);
    const row = sh.getLastRow() >= 2 ? sh.getRange(2, 1, 1, width).getValues()[0] : [];
    byTab[tab].forEach(([h, v]) => {
      let c = header.indexOf(h);
      if (c < 0) { header.push(h); c = header.length - 1; }
      row[c] = v;
      n++;
    });
    while (row.length < header.length) row.push('');
    sh.getRange(1, 1, 1, header.length).setValues([header]).setFontWeight('bold');
    sh.getRange(2, 1, 1, header.length).setValues([row.slice(0, header.length)]);
    const t = header.indexOf('Last Update Time');
    if (t >= 0) sh.getRange(2, t + 1).setNumberFormat('yyyy/MM/dd H:mm:ss');
  });
  return {ok: true, values: n};
}

/** Snapshot every status_now tab into one Log row (production column layout). */
function appendLog_() {
  const ss = SpreadsheetApp.getActive();
  const values = {};
  ss.getSheets().forEach(sh => {
    if (sh.getLastRow() < 2 || sh.getName().charAt(0) === '_') return;
    const [header, row] = sh.getRange(1, 1, 2, sh.getLastColumn()).getValues();
    header.forEach((h, i) => { if (h) values[sh.getName() + '_' + h] = row[i]; });
  });
  if (!Object.keys(values).length) return {ok: false, error: 'status_now is empty'};
  const outlet = values['MPHUB_Outlet Pressure (bar)'];
  if (typeof outlet === 'number') values['MPHUB_Equiv. Vol (L)'] = (outlet - 1) * TANK_L_PER_BAR;
  values['Total Volume (L)'] = VOLUME_COLS.reduce((s, k) => s + (typeof values[k] === 'number' ? values[k] : 0), 0);
  values['Log Time'] = new Date();

  const book = logBook_();
  const log = book.getSheetByName(LOG_TAB);
  archiveIfNewMonth_(book, log);
  const header = log.getRange(1, 1, 1, log.getLastColumn()).getValues()[0];
  const extra = Object.keys(values).filter(k => header.indexOf(k) < 0);
  if (extra.length) {
    log.getRange(1, header.length + 1, 1, extra.length).setValues([extra]).setFontWeight('bold');
    header.push(...extra);
  }
  log.appendRow(header.map(h => (h in values ? values[h] : '')));
  const r = log.getLastRow();
  header.forEach((h, i) => {
    if (h === 'Log Time' || /Last Update Time$/.test(h)) log.getRange(r, i + 1).setNumberFormat('yyyy/MM/dd H:mm:ss');
  });
  return {ok: true, row: r, newColumns: extra.length};
}

/**
 * On the first log of a new month, move the Log rows to <ARCHIVE_NAME>_YYYYMM in the archive
 * folder and start fresh. If that file already exists (an earlier attempt), the rows are added to it.
 */
function archiveIfNewMonth_(book, log) {
  if (log.getLastRow() < 2) return;
  const last = log.getRange(log.getLastRow(), 1).getValue();
  const now = new Date();
  if (!(last instanceof Date) || (last.getMonth() === now.getMonth() && last.getFullYear() === now.getFullYear())) return;
  const name = ARCHIVE_NAME + '_' + Utilities.formatDate(last, Session.getScriptTimeZone(), 'yyyyMM');
  const folder = archiveFolder_(book);
  const found = folder.getFilesByName(name);
  if (found.hasNext()) {
    const target = SpreadsheetApp.openById(found.next().getId()).getSheetByName(LOG_TAB);
    const rows = log.getRange(2, 1, log.getLastRow() - 1, log.getLastColumn()).getValues();
    target.getRange(target.getLastRow() + 1, 1, rows.length, rows[0].length).setValues(rows);
  } else {
    const archive = SpreadsheetApp.create(name);
    log.copyTo(archive).setName(LOG_TAB);
    archive.deleteSheet(archive.getSheets()[0]);
    DriveApp.getFileById(archive.getId()).moveTo(folder);
  }
  log.deleteRows(2, log.getLastRow() - 1);
}

/** ARCHIVE_FOLDER_ID, else a "LogArchives" folder next to the log file (created if missing). */
function archiveFolder_(book) {
  if (ARCHIVE_FOLDER_ID) return DriveApp.getFolderById(ARCHIVE_FOLDER_ID);
  const parent = DriveApp.getFileById(book.getId()).getParents().next();
  const folders = parent.getFoldersByName(ARCHIVE_FOLDER);
  return folders.hasNext() ? folders.next() : parent.createFolder(ARCHIVE_FOLDER);
}

// Other spreadsheets the lab PC may read (header row + value row of one tab), never write.
const READ_SOURCES = (PROPS.getProperty('READ_SOURCES') || '')
  .split(',').map(s => s.trim()).filter(String);

/** Read row 1 (headers) and row 2 (values) of tab `gid` in this file or an allow-listed spreadsheet. */
function readSheet_(id, gid) {
  if (id !== SpreadsheetApp.getActive().getId() && READ_SOURCES.indexOf(id) < 0) return {ok: false, error: 'sheet not allowed'};
  const sh = SpreadsheetApp.openById(id).getSheets().find(s => String(s.getSheetId()) === String(gid));
  if (!sh) return {ok: false, error: 'no tab with gid ' + gid};
  const width = sh.getLastColumn();
  if (sh.getLastRow() < 2 || !width) return {ok: false, error: 'tab ' + sh.getName() + ' is empty'};
  const [header, row] = sh.getRange(1, 1, 2, width).getValues();
  const values = {};
  header.forEach((h, i) => { if (h) values[h] = cell_(row[i]); });
  return {ok: true, name: sh.getName(), values: values};
}

/** Test hook: status_now tabs as {tab: {header: value}} and the last `rows` Log rows. */
function dump_(rows) {
  const status = {};
  SpreadsheetApp.getActive().getSheets().forEach(sh => {
    if (sh.getLastRow() < 2) return;
    const [header, row] = sh.getRange(1, 1, 2, sh.getLastColumn()).getValues();
    status[sh.getName()] = {};
    header.forEach((h, i) => { if (h) status[sh.getName()][h] = cell_(row[i]); });
  });
  const log = logBook_().getSheetByName(LOG_TAB);
  const out = {ok: true, status: status, log: [], logRows: log ? log.getLastRow() : 0};
  if (log && log.getLastRow() >= 2) {
    const header = log.getRange(1, 1, 1, log.getLastColumn()).getValues()[0];
    const first = Math.max(2, log.getLastRow() - rows + 1);
    log.getRange(first, 1, log.getLastRow() - first + 1, header.length).getValues().forEach((r, k) => {
      const o = {_row: first + k};
      header.forEach((h, i) => { if (h && r[i] !== '') o[h] = cell_(r[i]); });
      out.log.push(o);
    });
  }
  return out;
}

/** Test hook: delete a test tab, its Log columns, and (optionally) one Log row. */
function cleanup_(tab, row) {
  if (!tab || !/^ZZTEST/.test(tab)) return {ok: false, error: 'cleanup only accepts ZZTEST* tabs'};
  const ss = SpreadsheetApp.getActive();
  const sh = ss.getSheetByName(tab);
  if (sh) ss.deleteSheet(sh);
  const log = logBook_().getSheetByName(LOG_TAB);
  let removed = 0;
  if (log && log.getLastColumn() > 0) {
    if (row >= 2 && row <= log.getLastRow()) log.deleteRow(row);
    const header = log.getRange(1, 1, 1, log.getLastColumn()).getValues()[0];
    for (let i = header.length - 1; i >= 0; i--) {
      if (String(header[i]).indexOf(tab + '_') === 0) { log.deleteColumn(i + 1); removed++; }
    }
  }
  return {ok: true, removedColumns: removed};
}

// --- Helium transfer log ("Helium Recovery System Operation", next to status_log) ---------
// Transfer Step 3 sends action=transfer with the fields below; one row per transfer.
const OPERATION_BOOK = 'Helium Recovery System Operation';
const TRANSFER_TAB = 'Helium Transfer';
const TRANSFER_FIELDS = [
  ['date', 'Date'], ['start', 'Start Time'], ['end', 'End Time'], ['operator', 'Operator'],
  ['atl', 'ATL'], ['atl_start', 'ATL Start Level (L)'], ['atl_end', 'ATL End Level (L)'],
  ['cryostat', 'Cryostat'], ['cryo_start', 'Cryostat Start Level'], ['cryo_end', 'Cryostat End Level'],
  ['cryo_unit', 'Cryostat Level Unit'], ['notes', 'Notes'], ['source', 'Logged By']];
const TRANSFER_HEADER = ['Logged At'].concat(TRANSFER_FIELDS.map(f => f[1]));

/** The operation spreadsheet: remembered id, else found or created in status_log's folder. */
function operationBook_() {
  const props = PropertiesService.getScriptProperties();
  const id = props.getProperty('OPERATION_SHEET_ID');
  if (id) {
    try { return SpreadsheetApp.openById(id); } catch (e) { /* deleted: find or create again */ }
  }
  const folder = DriveApp.getFileById(LOG_SHEET_ID || SpreadsheetApp.getActive().getId()).getParents().next();
  const found = folder.getFilesByName(OPERATION_BOOK);
  let book;
  if (found.hasNext()) {
    book = SpreadsheetApp.openById(found.next().getId());
  } else {
    book = SpreadsheetApp.create(OPERATION_BOOK);
    DriveApp.getFileById(book.getId()).moveTo(folder);
  }
  props.setProperty('OPERATION_SHEET_ID', book.getId());
  return book;
}

function transferTabName_(tab) {
  return tab && /^ZZTEST/.test(tab) ? tab : TRANSFER_TAB; // tests write to ZZTEST* tabs only
}

function transferSheet_(book, name) {
  let sh = book.getSheetByName(name);
  if (!sh) {
    sh = book.insertSheet(name);
    sh.getRange(1, 1, 1, TRANSFER_HEADER.length).setValues([TRANSFER_HEADER]).setFontWeight('bold');
    sh.setFrozenRows(1);
    const first = book.getSheetByName('Sheet1');
    if (first && first.getLastRow() === 0) book.deleteSheet(first);
  }
  return sh;
}

function logTransfer_(p) {
  const book = operationBook_();
  const sh = transferSheet_(book, transferTabName_(p.tab));
  const header = sh.getRange(1, 1, 1, sh.getLastColumn()).getValues()[0];
  const values = {'Logged At': new Date()};
  TRANSFER_FIELDS.forEach(([key, h]) => { if (p[key] !== undefined) values[h] = /level/i.test(h) ? parse_(p[key]) : p[key]; });
  sh.appendRow(header.map(h => (h in values ? values[h] : '')));
  const r = sh.getLastRow();
  sh.getRange(r, header.indexOf('Logged At') + 1).setNumberFormat('yyyy/MM/dd H:mm:ss');
  return {ok: true, row: r, sheet: book.getName(), id: book.getId(), tab: sh.getName()};
}

/** Operators of earlier transfers, most recent first (for the Step 3 combobox). */
function operators_(tab) {
  const sh = operationBook_().getSheetByName(transferTabName_(tab));
  if (!sh || sh.getLastRow() < 2) return {ok: true, operators: []};
  const col = sh.getRange(1, 1, 1, sh.getLastColumn()).getValues()[0].indexOf('Operator') + 1;
  if (!col) return {ok: true, operators: []};
  const names = sh.getRange(2, col, sh.getLastRow() - 1, 1).getValues().map(r => String(r[0]).trim()).reverse();
  return {ok: true, operators: names.filter((n, i) => n && names.indexOf(n) === i)};
}

function transferCleanup_(tab) {
  if (!tab || !/^ZZTEST/.test(tab)) return {ok: false, error: 'transfer_cleanup only accepts ZZTEST* tabs'};
  const book = operationBook_();
  transferSheet_(book, TRANSFER_TAB); // a spreadsheet cannot lose its last tab
  const sh = book.getSheetByName(tab);
  if (sh) book.deleteSheet(sh);
  return {ok: true, removed: !!sh};
}

function cell_(v) {
  return v instanceof Date ? Utilities.formatDate(v, Session.getScriptTimeZone(), "yyyy/MM/dd HH:mm:ss") : v;
}

/** Charts on the log book: [title, unit, Log column names]. */
const CHART_SPECS = [
  ['Total helium volume', 'L', ['Total Volume (L)']],
  ['ATL liquid volume', 'L', ['ATL1_Liquid Vol (L)', 'ATL2_Liquid Vol (L)', 'ATL3_Liquid Vol (L)']],
  ['ATL diode temperature', 'K', ['ATL1_Diorde T (K)', 'ATL3_Diorde T (K)']],
  ['ATL dewar pressure', 'psi', ['ATL1_Dewar Pres (psi)', 'ATL2_Dewar Pres (psi)', 'ATL3_Dewar Pres (psi)']],
  ['MP Hub pressures', 'bar', ['MPHUB_Inlet Pressure (bar)', 'MPHUB_Tank Pressure (bar)', 'MPHUB_Outlet Pressure (bar)']],
  ['Janis LHe level', 'in', ['Janis_LHe Level (in)']],
];

function buildCharts() {
  const book = logBook_();
  const log = book.getSheetByName(LOG_TAB);
  const charts = book.getSheetByName(CHART_TAB) || book.insertSheet(CHART_TAB);
  charts.getCharts().forEach(c => charts.removeChart(c));
  const header = log.getRange(1, 1, 1, log.getLastColumn()).getValues()[0];
  const rows = Math.max(log.getMaxRows(), 2);
  let slot = 0;
  CHART_SPECS.forEach(([title, unit, cols]) => {
    const idx = cols.map(c => header.indexOf(c) + 1).filter(i => i > 0);
    if (!idx.length) return;
    let b = charts.newChart().asLineChart().addRange(log.getRange(1, 1, rows, 1));
    idx.forEach(i => (b = b.addRange(log.getRange(1, i, rows, 1))));
    charts.insertChart(b.setNumHeaders(1)
      .setOption('title', title).setOption('vAxis', {title: unit})
      .setOption('hAxis', {format: 'MM/dd HH:mm'}).setOption('legend', {position: 'bottom'})
      .setOption('interpolateNulls', true).setOption('width', 640).setOption('height', 320)
      .setPosition(1 + Math.floor(slot / 2) * 17, 1 + (slot % 2) * 7, 0, 0).build());
    slot++;
  });
  Logger.log('built ' + slot + ' charts');
  return slot;
}

function parse_(v) {
  if (v === undefined || v === '') return '';
  if (v === 'TRUE' || v === 'FALSE') return v === 'TRUE';
  if (/^\d{4}\/\d{2}\/\d{2} \d{2}:\d{2}:\d{2}$/.test(v)) return new Date(v.replace(/\//g, '-').replace(' ', 'T'));
  const n = Number(v);
  return v.trim() !== '' && !isNaN(n) ? n : v;
}

function logBook_() {
  return LOG_SHEET_ID ? SpreadsheetApp.openById(LOG_SHEET_ID) : SpreadsheetApp.getActive();
}

function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

// --- Graphic View page (GraphicView.html) ----------------------------------------------------
const TREND_HOURS = 24;
const TREND_COLS = {inlet: 'MPHUB_Inlet Pressure (bar)', setpoint: 'MPHUB_Inlet Pressure Setpoint (mbar)',
  tank: 'MPHUB_Tank Pressure (bar)'};

/**
 * Data for the Graphic View page (called from the page with google.script.run):
 * every status_now tab as {header: value} (times as ms since 1970), and the last
 * TREND_HOURS of inlet / setpoint / tank pressure from the Log (bar).
 */
function graphicView(key) {
  const want = PROPS.getProperty('VIEW_KEY');
  if (want && key !== want) throw new Error('Not authorised');
  const plain = v => (v instanceof Date ? v.getTime() : v);
  const tabs = {};
  SpreadsheetApp.getActive().getSheets().forEach(sh => {
    if (sh.getLastRow() < 2 || !sh.getLastColumn()) return;
    const [header, row] = sh.getRange(1, 1, 2, sh.getLastColumn()).getValues();
    const o = tabs[sh.getName()] = {};
    header.forEach((h, i) => { if (h) o[h] = plain(row[i]); });
  });
  return {now: new Date().getTime(), tabs: tabs, trend: trend_()};
}

function trend_() {
  const out = {t: [], inlet: [], setpoint: [], tank: []};
  const log = logBook_().getSheetByName(LOG_TAB);
  if (!log || log.getLastRow() < 2) return out;
  const header = log.getRange(1, 1, 1, log.getLastColumn()).getValues()[0];
  const n = Math.min(log.getLastRow() - 1, Math.ceil(TREND_HOURS * 60 / 3) + 20);
  const first = log.getLastRow() - n + 1;
  const col = name => {
    const c = header.indexOf(name);
    return c < 0 ? Array(n).fill('') : log.getRange(first, c + 1, n, 1).getValues().map(r => r[0]);
  };
  const t = col('Log Time'), inlet = col(TREND_COLS.inlet), sp = col(TREND_COLS.setpoint), tank = col(TREND_COLS.tank);
  const since = new Date().getTime() - TREND_HOURS * 3600e3;
  const num = v => (typeof v === 'number' && isFinite(v) ? v : null);
  t.forEach((v, i) => {
    if (!(v instanceof Date) || v.getTime() < since) return;
    out.t.push(v.getTime());
    out.inlet.push(num(inlet[i]));
    out.setpoint.push(num(sp[i]) === null ? null : sp[i] / 1000);
    out.tank.push(num(tank[i]));
  });
  return out;
}
