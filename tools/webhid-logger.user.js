// ==UserScript==
// @name         Scyrox WebHID logger (structured)
// @namespace    scyrox-config
// @match        https://www.scyrox.net/*
// @run-at       document-start
// @grant        none
// @version      2.0
// ==/UserScript==
//
// Logs every WebHID exchange on the official Scyrox driver and lets you export a
// structured JSON file (so you don't depend on console scrollback). It also
// annotates ReadFlashData/WriteFlashData frames with their flash address/length,
// matching PROTOCOL.md, to make a capture session readable.
//
//   Ctrl+Shift+L  -> download scyrox-hid-<timestamp>.json
//   Ctrl+Shift+K  -> clear the buffer (start a fresh capture for one setting)
//   window.scyroxLog.dump() / .clear() / .entries  also available in the console.
(function () {
  const REPORT_ID = 0x08;
  const CMD = {
    1: 'EncryptionData', 2: 'PCDriverStatus', 3: 'DeviceOnLine', 4: 'BatteryLevel',
    5: 'DongleEnterPair', 6: 'GetPairState', 7: 'WriteFlashData', 8: 'ReadFlashData',
    9: 'ClearSetting', 10: 'StatusChanged', 14: 'GetCurrentConfig', 15: 'SetCurrentConfig',
    18: 'ReadVersionID', 20: 'Set4KDongleRGB', 21: 'Get4KDongleRGB', 22: 'SetLongRangeMode',
    23: 'GetLongRangeMode', 24: 'SetDongleRGBBar', 25: 'GetDongleRGBBar', 29: 'GetDongleVersion',
    44: 'SetDongle3RGB', 45: 'GetDongle3RGB', 183: 'OfficeCustomLightState',
  };

  const toU8 = b =>
    b instanceof DataView ? new Uint8Array(b.buffer, b.byteOffset, b.byteLength)
    : b instanceof ArrayBuffer ? new Uint8Array(b) : new Uint8Array(b);
  const hex = b => Array.from(toU8(b), x => x.toString(16).padStart(2, '0')).join(' ');

  const entries = [];
  const log = (...a) => console.log('%c[HID]', 'color:#f80;font-weight:bold',
    performance.now().toFixed(1) + 'ms', ...a);

  // Annotate a frame. For sendReport, `id` is the report id and `data` is the
  // 16-byte command buffer (cmd@0). For input/feature reports the layout is
  // shifted (report id is byte 0 of `data`).
  function annotate(dir, id, data) {
    const u = toU8(data);
    let cmd, flash;
    if (dir === 'out' && id === REPORT_ID) {
      cmd = u[0];
      if (cmd === 7 || cmd === 8) {
        flash = { addr: (u[2] << 8) | u[3], len: u[4] & 0x0f };
        if (cmd === 7) flash.data = Array.from(u.slice(5, 5 + flash.len));
      }
    } else if (dir === 'in' && id === REPORT_ID) {
      cmd = u[1];
      if (cmd === 7 || cmd === 8) flash = { addr: (u[2] << 8) | u[3], len: u[3 + 1] & 0x0f };
    }
    return { cmd, cmdName: CMD[cmd], flash };
  }

  function record(dir, kind, id, data) {
    const a = annotate(dir, id, data);
    const e = {
      t: +performance.now().toFixed(1),
      dir, kind,
      reportId: '0x' + (id ?? 0).toString(16).padStart(2, '0'),
      bytes: hex(data),
      cmd: a.cmdName || (a.cmd != null ? a.cmd : undefined),
      flash: a.flash,
    };
    entries.push(e);
    log(`${dir === 'out' ? '->' : '<-'} ${kind}`,
      e.cmd ? `[${e.cmd}${a.flash ? ` @${a.flash.addr} len${a.flash.len}` : ''}]` : '',
      e.bytes);
  }

  const dump = () => {
    const blob = new Blob([JSON.stringify({ capturedAt: Date.now(), entries }, null, 2)],
      { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `scyrox-hid-${Date.now()}.json`;
    a.click();
    URL.revokeObjectURL(url);
    log(`exported ${entries.length} entries`);
  };
  window.scyroxLog = { entries, dump, clear: () => { entries.length = 0; log('cleared'); } };

  addEventListener('keydown', e => {
    if (e.ctrlKey && e.shiftKey && e.code === 'KeyL') { e.preventDefault(); dump(); }
    if (e.ctrlKey && e.shiftKey && e.code === 'KeyK') { e.preventDefault(); window.scyroxLog.clear(); }
  });

  if (!navigator.hid) return log('no WebHID on this page');
  const seen = new WeakSet();
  const hook = d => {
    if (seen.has(d)) return; seen.add(d);
    log('device', d.productName, d.vendorId.toString(16) + ':' + d.productId.toString(16));
    d.addEventListener('inputreport', e => record('in', 'input', e.reportId, e.data));
    for (const m of ['sendReport', 'sendFeatureReport']) {
      const f = d[m].bind(d);
      d[m] = (id, data) => { record('out', m, id, data); return f(id, data); };
    }
    const rf = d.receiveFeatureReport.bind(d);
    d.receiveFeatureReport = async id => {
      const r = await rf(id); record('in', 'feature', id, r); return r;
    };
  };
  for (const m of ['requestDevice', 'getDevices']) {
    const f = navigator.hid[m].bind(navigator.hid);
    navigator.hid[m] = async (...a) => { const ds = await f(...a); ds.forEach(hook); return ds; };
  }
  log('installed — Ctrl+Shift+L to export, Ctrl+Shift+K to clear');
})();
