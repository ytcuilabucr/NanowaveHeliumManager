// In-memory fakes of the Apps Script services used by apps_script/HeliumStatus.gs.
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Range {
  constructor(sheet, r, c, nr = 1, nc = 1) { Object.assign(this, {sheet, r, c, nr, nc}); }
  getValues() {
    const out = [];
    for (let i = 0; i < this.nr; i++) {
      const row = this.sheet.data[this.r - 1 + i] || [];
      out.push(Array.from({length: this.nc}, (_, j) => (row[this.c - 1 + j] ?? '')));
    }
    return out;
  }
  getValue() { return this.getValues()[0][0]; }
  setValues(v) {
    v.forEach((row, i) => row.forEach((x, j) => this.sheet.set(this.r + i, this.c + j, x)));
    return this;
  }
  setValue(x) { return this.setValues([[x]]); }
  clearContent() { return this.setValues(Array.from({length: this.nr}, () => Array(this.nc).fill(''))); }
  setNumberFormat() { return this; }
  setFontWeight() { return this; }
}

class Sheet {
  constructor(book, name) { Object.assign(this, {book, name, data: [], charts: [], gid: Sheet.nextGid++}); }
  getSheetId() { return this.gid; }
  getName() { return this.name; }
  setName(n) { this.name = n; return this; }
  set(r, c, x) {
    while (this.data.length < r) this.data.push([]);
    const row = this.data[r - 1];
    while (row.length < c) row.push('');
    row[c - 1] = x;
  }
  getLastRow() {
    for (let i = this.data.length; i > 0; i--) if (this.data[i - 1].some(x => x !== '')) return i;
    return 0;
  }
  getLastColumn() {
    let m = 0;
    this.data.forEach(row => row.forEach((x, j) => { if (x !== '') m = Math.max(m, j + 1); }));
    return m;
  }
  getMaxRows() { return Math.max(this.data.length, 1000); }
  getRange(r, c, nr, nc) { return new Range(this, r, c, nr, nc); }
  appendRow(row) { this.data.splice(this.getLastRow(), 0, [...row]); }
  deleteRow(r) { this.data.splice(r - 1, 1); }
  deleteRows(r, n) { this.data.splice(r - 1, n); }
  deleteColumn(c) { this.data.forEach(row => row.splice(c - 1, 1)); }
  setFrozenRows() {}
  setFrozenColumns() {}
  getCharts() { return [...this.charts]; }
  removeChart(c) { this.charts = this.charts.filter(x => x !== c); }
  insertChart(c) { this.charts.push(c); }
  newChart() {
    const spec = {ranges: [], options: {}};
    const b = {
      asLineChart: () => b, addRange: r => (spec.ranges.push(r), b), setNumHeaders: () => b,
      setOption: (k, v) => ((spec.options[k] = v), b), setPosition: () => b, build: () => spec,
    };
    return b;
  }
  copyTo(book) { const s = book.insertSheet(this.name); s.data = this.data.map(r => [...r]); return s; }
}

Sheet.nextGid = 1000;

class Book {
  constructor(id, name) { Object.assign(this, {id, name, sheets: [new Sheet(this, 'Sheet1')]}); }
  getId() { return this.id; }
  getName() { return this.name; }
  getSheets() { return [...this.sheets]; }
  getSheetByName(n) { return this.sheets.find(s => s.name === n) || null; }
  insertSheet(n) { const s = new Sheet(this, n); this.sheets.push(s); return s; }
  deleteSheet(s) { this.sheets = this.sheets.filter(x => x !== s); }
}

/** Load HeliumStatus.gs into a fresh sandbox. Returns {ctx, status, log, books, props, now}. */
function loadScript({now = new Date('2026-10-09T10:00:00'), properties = {}} = {}) {
  const status = new Book('status-id', 'status_now');
  const books = {'status-id': status};
  const props = {LOG_SHEET_ID: 'test-log-sheet', READ_SOURCES: 'test-cryo-sheet', ...properties};
  const moved = [];
  const folders = {};
  const iter = list => { let i = 0; return {hasNext: () => i < list.length, next: () => list[i++]}; };
  // Drive folders by name; a folder lists the books moved into it.
  const folder = name => folders[name] || (folders[name] = {
    name,
    getName: () => name,
    getFoldersByName: n => iter(folders[n] ? [folders[n]] : []),
    getFilesByName: n => iter(Object.values(books).filter(b => b.folder === name && b.name === n)),
    createFolder: n => folder(n),
  });
  let clock = now;
  const FakeDate = class extends Date {
    constructor(...a) { super(...(a.length ? a : [clock.getTime()])); }
  };
  const ctx = {
    Date: FakeDate,
    Math, JSON, Number, String, Object, Array, isNaN,
    console,
    SpreadsheetApp: {
      getActive: () => status,
      openById: id => {
        if (!books[id]) books[id] = new Book(id, 'status_log');
        return books[id];
      },
      create: name => { const b = new Book('archive-' + name, name); books[b.id] = b; return b; },
    },
    PropertiesService: {getScriptProperties: () => ({
      getProperty: k => props[k] ?? null, setProperty: (k, v) => { props[k] = v; },
    })},
    LockService: {getScriptLock: () => ({waitLock() {}, releaseLock() {}})},
    ContentService: {
      MimeType: {JSON: 'json'},
      createTextOutput: text => ({setMimeType: () => ({text, json: JSON.parse(text)})}),
    },
    Utilities: {
      getUuid: () => 'uuid-1',
      formatDate: (d, tz, fmt) => {
        const p = n => String(n).padStart(2, '0');
        return fmt === 'yyyyMM' ? `${d.getFullYear()}${p(d.getMonth() + 1)}`
          : `${d.getFullYear()}/${p(d.getMonth() + 1)}/${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
      },
    },
    Session: {getScriptTimeZone: () => 'America/Los_Angeles'},
    Logger: {log() {}},
    DriveApp: {
      getFileById: id => ({
        getParents: () => ({next: () => folder('status_log folder')}),
        moveTo: f => { moved.push({id, folder: f.name}); if (books[id]) books[id].folder = f.name; },
      }),
      getFolderById: id => folder('folder ' + id),
    },
  };
  vm.createContext(ctx);
  const code = fs.readFileSync(path.join(__dirname, '..', '..', 'apps_script', 'HeliumStatus.gs'), 'utf8');
  vm.runInContext(code, ctx);
  return {
    ctx, status, books, props, moved, folder,
    setNow: d => { clock = d; },
    logBook: () => ctx.SpreadsheetApp.openById(vm.runInContext('LOG_SHEET_ID', ctx) || 'status-id'),
    post: params => ctx.doPost({parameter: params}).json,
  };
}

module.exports = {loadScript};
