/**
 * Canal Booker: Google Sheet helper (Apps Script).
 *
 * Install once (the sheet owner):
 *   1. In the team plan sheet: Extensions > Apps Script. Delete what is there, paste this file, Save.
 *   2. Pick "setup" in the function list at the top and click Run. Allow access when Google asks.
 *      This adds the "Room bookings" page (first tab), the "Setup guide" and "Status" tabs.
 *   3. Deploy > New deployment > type "Web app". Execute as: Me. Who has access: Anyone. Deploy.
 *      Copy the Web app URL (ends in /exec) and put it in app_defaults.json as "status_url".
 *
 * The booker then posts every result to the Status tab: booked, not booked, sign-in failed,
 * test runs. It only accepts results for usernames that are in the plan tab, and it never
 * receives or stores passwords.
 *
 * "Room bookings", the first tab, is the page for everyone: this week's and next week's bookings
 * and a list of all of them, rebuilt after every result and once a night. It is filled in by this
 * script only (protected), like the Status tab. To let everyone look but only the people running
 * the booker change the plan: Share > General access "Anyone with the link" as Viewer, and add the
 * people who run the booker as Editors.
 */

var REPO = 'https://github.com/silix54/canal-booker';
var STATUS = 'Status';
var GUIDE = 'Setup guide';
var BOOKINGS = 'Room bookings';
var TZ = 'America/Toronto';
var HEADERS = ['When (Ottawa)', 'Name', 'Username', 'For date', 'Room', 'Time', 'Result', 'Details', 'Ran from'];

function setup() {
  var ss = SpreadsheetApp.getActive();
  makeGuide_(ss);
  makeStatus_(ss);
  setupDropdowns();
  refreshBookings();
  protect_(ss.getSheetByName(STATUS));
  // Rebuild the bookings page once a night too, so "this week" moves on even without new results.
  var has = ScriptApp.getProjectTriggers().some(function (t) { return t.getHandlerFunction() === 'refreshBookings'; });
  if (!has) ScriptApp.newTrigger('refreshBookings').timeBased().everyDays(1).atHour(0).nearMinute(30).inTimezone(TZ).create();
  ss.setActiveSheet(ss.getSheetByName(BOOKINGS));
  ss.toast('Room bookings page, Setup guide, Status tab and dropdowns are ready.');
}

/** Adds a Canal Booker menu to the sheet, so the organizer can redo things without opening the script. */
function onOpen() {
  SpreadsheetApp.getUi().createMenu('Canal Booker')
    .addItem('Refresh the bookings page', 'refreshBookings')
    .addItem('Set up dropdowns', 'setupDropdowns')
    .addToUi();
}

var ROOMS = ['CB 2103', 'CB 2302', 'CB 3102', 'CB 3201', 'CB 3208'];
var DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
var PLAN_HEADERS = ['Name', 'Username', 'Day', 'Start', 'End', 'Backup 1', 'Backup 2', 'Room 1', 'Room 2', 'Room 3'];
var PLAN_ROWS = 200;

/**
 * Turns the plan tab into dropdowns: Day, Start, End, two backup times and three rooms in order.
 * An older plan (Backup times / Rooms (in order) typed as lists) is converted first, keeping every value.
 * Safe to run again.
 */
function setupDropdowns() {
  fixTimeZone_();
  var found = findPlan_();
  if (!found) {
    SpreadsheetApp.getActive().toast('No plan tab found (a row with Username and Day).');
    return;
  }
  var sh = found.sheet, head = found.row; // head is 1-based
  var lastCol = Math.max(sh.getLastColumn(), PLAN_HEADERS.length);
  var lastRow = Math.max(sh.getLastRow(), head);
  var header = sh.getRange(head, 1, 1, lastCol).getDisplayValues()[0].map(function (v) { return String(v).trim().toLowerCase(); });
  var data = lastRow > head ? sh.getRange(head + 1, 1, lastRow - head, lastCol).getDisplayValues() : [];

  function pick(row, prefix) {
    var out = [];
    header.forEach(function (h, j) {
      if (h.indexOf(prefix) === 0 && row[j]) {
        String(row[j]).split(',').forEach(function (p) { if (p.trim()) out.push(p.trim()); });
      }
    });
    return out;
  }
  function first(row, prefix) {
    var j = header.findIndex(function (h) { return h.indexOf(prefix) === 0; });
    return j < 0 ? '' : String(row[j]).trim();
  }
  function hhmm(t) {
    var m = String(t).trim().match(/^(\d{1,2}):(\d{2})/);
    return m ? ('0' + m[1]).slice(-2) + ':' + m[2] : String(t).trim();
  }

  var rows = data.filter(function (r) { return r.join('').trim(); }).map(function (r) {
    var backups = pick(r, 'backup').map(function (b) { return b.split('-').map(hhmm).join('-'); });
    var rooms = pick(r, 'room');
    return [first(r, 'name'), first(r, 'username'), first(r, 'day').substring(0, 3), hhmm(first(r, 'start')),
            hhmm(first(r, 'end')), backups[0] || '', backups[1] || '', rooms[0] || '', rooms[1] || '', rooms[2] || ''];
  });

  // Rewrite the header and the rows in the new layout.
  sh.getRange(head, 1, Math.max(lastRow - head + 1, 1), lastCol).clearDataValidations().clearContent();
  sh.getRange(head, 1, 1, PLAN_HEADERS.length).setValues([PLAN_HEADERS])
    .setFontWeight('bold').setBackground('#f3f3f3');
  sh.getRange(head + 1, 3, PLAN_ROWS, 8).setNumberFormat('@'); // keep 12:00 as text, not a date
  if (rows.length) sh.getRange(head + 1, 1, rows.length, PLAN_HEADERS.length).setValues(rows);

  var times = [];
  for (var m = 7 * 60; m <= 23 * 60; m += 30) times.push(('0' + Math.floor(m / 60)).slice(-2) + ':' + ('0' + m % 60).slice(-2));
  var ranges = [];
  for (var s = 7 * 60; s < 23 * 60; s += 30) {
    for (var len = 60; len <= 180; len += 30) {
      if (s + len <= 23 * 60) ranges.push(times[(s - 420) / 30] + '-' + times[(s + len - 420) / 30]);
    }
  }
  function list(values, help) {
    return SpreadsheetApp.newDataValidation().requireValueInList(values, true).setAllowInvalid(false)
      .setHelpText(help).build();
  }
  sh.getRange(head + 1, 3, PLAN_ROWS, 1).setDataValidation(list(DAYS, 'Pick a day.'));
  sh.getRange(head + 1, 4, PLAN_ROWS, 2).setDataValidation(list(times, 'Pick a time. At most 3 hours from Start to End.'));
  sh.getRange(head + 1, 6, PLAN_ROWS, 2).setDataValidation(list(ranges, 'Optional. Tried if the main time is taken.'));
  sh.getRange(head + 1, 8, PLAN_ROWS, 3).setDataValidation(list(ROOMS, 'Optional. Room 1 is tried first.'));

  var widths = [120, 120, 70, 80, 80, 120, 120, 100, 100, 100];
  for (var i = 0; i < widths.length; i++) sh.setColumnWidth(i + 1, widths[i]);
  if (head > 4) { // rows 1-4 are the instructions above the header
    sh.getRange(1, 1, 4, 1).setValues([
      ['Canal Booker team plan. One row per person per day. At most 3 hours a day.'],
      ['Type your Name and Username (your MyCarletonOne username, before @carleton.ca). Pick everything else from the dropdowns.'],
      ['Backup 1 and 2 are tried in order if the main time is taken. Room 1 is tried first, then Room 2, then Room 3 (all your times in a room before the next room).'],
      ['Leave the rooms blank to use your other rows\' rooms. Do not rename the header row below.'],
    ]);
  }
  SpreadsheetApp.getActive().toast('Dropdowns are ready on the "' + sh.getName() + '" tab.');
}

/**
 * Show times in Ottawa time. New sheets default to Pacific time, which made the Status tab 3 hours
 * off. A sheet stores a time as it reads in the sheet's time zone when written, so changing the
 * zone alone would leave the rows already there 3 hours off: they are read first (as real moments)
 * and written back after the change, which puts them in Ottawa time too.
 */
function fixTimeZone_() {
  var ss = SpreadsheetApp.getActive();
  if (ss.getSpreadsheetTimeZone() === TZ) return;
  var s = ss.getSheetByName(STATUS);
  var when = s && s.getLastRow() > 1 ? s.getRange(2, 1, s.getLastRow() - 1, 1).getValues() : null;
  ss.setSpreadsheetTimeZone(TZ);
  SpreadsheetApp.flush();
  if (when) s.getRange(2, 1, when.length, 1).setValues(when);
}

/** The tab with the plan: the first one with a row containing Username and Day. */
function findPlan_() {
  var sheets = SpreadsheetApp.getActive().getSheets();
  for (var i = 0; i < sheets.length; i++) {
    var name = sheets[i].getName();
    if (name === STATUS || name === GUIDE || name === BOOKINGS) continue;
    var values = sheets[i].getRange(1, 1, Math.min(Math.max(sheets[i].getLastRow(), 1), 30), Math.max(sheets[i].getLastColumn(), 1)).getDisplayValues();
    for (var r = 0; r < values.length; r++) {
      var low = values[r].map(function (v) { return String(v).trim().toLowerCase(); });
      if (low.indexOf('username') >= 0 && low.indexOf('day') >= 0) return { sheet: sheets[i], row: r + 1 };
    }
  }
  return null;
}

function makeGuide_(ss) {
  // At the end: the Room bookings page is first, then the plan.
  var g = ss.getSheetByName(GUIDE) || ss.insertSheet(GUIDE, ss.getNumSheets());
  g.clear();
  var lines = [
    ['# Canal Booker: how to set it up'],
    ['Canal Booker books your Canal Building study room the second it opens (midnight, one week ahead), under your own Carleton account. Your rooms and times come from the plan tab in this sheet.'],
    [''],
    ['# Step 1: add your rows to the plan tab (everyone who runs the booker)'],
    ['Only people who run the booker can change the plan; ask the sheet owner to add you as an editor. Everyone else can look.'],
    ['One row per day you want. Type your Name and Username (your MyCarletonOne username, the part before @carleton.ca); pick Day, Start and End from the dropdowns. At most 3 hours a day.'],
    ['Backup 1 and Backup 2 (optional) are tried in order if the main time is taken. Room 1 to Room 3 (optional) are your rooms for that day in order of preference; every time is tried in Room 1 before Room 2.'],
    ['Do not rename the header row. Changes count if made before about 11:55 pm; the bot re-reads the sheet just before midnight.'],
    [''],
    ['# Step 2, option A: run it in the cloud (recommended, your computer can be off)'],
    ['1. Make a free GitHub account if you do not have one, and sign in.'],
    ['2. Open ' + REPO + ' and click Fork (top right), then Create fork.'],
    ['3. In your fork: Settings > Secrets and variables > Actions > New repository secret. Add CARLETON_USERNAME (your username) and CARLETON_PASSWORD (your password). Names must match exactly.'],
    ['4. Open the Actions tab and click "I understand my workflows, go ahead and enable them".'],
    ['5. Test: Actions > Book my room > Run workflow > mode "test" > Run workflow. After a couple of minutes the Status tab here shows "Test passed" for you. Nothing is booked by a test.'],
    ['6. Done. It runs every night by itself. To get emails for good news too: GitHub Settings > Notifications > Actions > untick "Only notify for failed workflows".'],
    ['Your password is stored as an encrypted GitHub secret. It is never shown in logs and never sent to this sheet.'],
    ['Updates are automatic: your copy runs the newest code from ' + REPO + ' every night. Copies made before Oct 6, 2026 need one last click: open your copy on GitHub > Sync fork > Update branch.'],
    [''],
    ['# Step 2, option B: run it on your computer'],
    ['1. Open ' + REPO + '/releases and download CanalBooker.exe (Windows) or CanalBooker-Mac.zip (Mac).'],
    ['2. Open it. Windows: if you see "Windows protected your PC", click More info > Run anyway. Mac: right click > Open the first time.'],
    ['3. Follow the 4 steps at the top of the page that opens: sign in, get your slots from this sheet, dry run, turn on.'],
    ['Keep the computer plugged in, awake and online at midnight. Use option A or option B, not both.'],
    [''],
    ['# Checking if you got your room'],
    ['The Room bookings tab (the first one) shows this week\'s and next week\'s bookings and every booking coming up, for everyone. It updates by itself after each booking.'],
    ['The Status tab has every result, newest at the top: Booked (green), Not booked or Sign-in failed (red), Test passed. The portal\'s My Bookings page always shows what you really have.'],
    ['Not booked? The room may already be taken. Book by hand on booking.carleton.ca, the rest of the week stays open.'],
    ['Please cancel any booking you will not use. Unused rooms block other students, and the portal shows who booked them.'],
  ];
  g.getRange(1, 1, lines.length, 1).setValues(lines);
  g.setColumnWidth(1, 900);
  g.getRange(1, 1, lines.length, 1).setWrap(true).setVerticalAlignment('top').setFontSize(11);
  for (var i = 0; i < lines.length; i++) {
    var text = lines[i][0];
    if (text.indexOf('# ') === 0) {
      g.getRange(i + 1, 1).setValue(text.substring(2)).setFontWeight('bold').setFontSize(i === 0 ? 16 : 13)
        .setBackground('#fbe9ec');
    }
  }
  g.setFrozenRows(1);
}

function makeStatus_(ss) {
  var s = ss.getSheetByName(STATUS) || ss.insertSheet(STATUS);
  if (s.getLastRow() === 0) s.appendRow(HEADERS);
  s.getRange(1, 1, 1, HEADERS.length).setFontWeight('bold').setBackground('#f3f3f3');
  s.setFrozenRows(1);
  var widths = [150, 110, 110, 100, 90, 110, 130, 520, 90];
  for (var i = 0; i < widths.length; i++) s.setColumnWidth(i + 1, widths[i]);
  s.getRange('A:A').setNumberFormat('ddd mmm d, h:mm am/pm');
  var result = s.getRange('G2:G');
  s.setConditionalFormatRules([
    SpreadsheetApp.newConditionalFormatRule().whenTextStartsWith('Booked').setBackground('#d9f2e3').setRanges([result]).build(),
    SpreadsheetApp.newConditionalFormatRule().whenTextStartsWith('Test passed').setBackground('#e3edfb').setRanges([result]).build(),
    SpreadsheetApp.newConditionalFormatRule().whenTextContains('failed').setBackground('#fbe0e0').setRanges([result]).build(),
    SpreadsheetApp.newConditionalFormatRule().whenTextStartsWith('Not').setBackground('#fbe0e0').setRanges([result]).build(),
  ]);
}

/** The booker posts results here as JSON. */
function doPost(e) {
  var d;
  try {
    d = JSON.parse((e && e.postData && e.postData.contents) || '{}');
  } catch (err) {
    return reply_('bad json');
  }
  var user = clean_(d.username).toLowerCase();
  if (!user || !inPlan_(user)) return reply_('unknown username');
  var s = SpreadsheetApp.getActive().getSheetByName(STATUS);
  if (!s) return reply_('run setup first');
  var lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    s.insertRowAfter(1);
    s.getRange(2, 1, 1, HEADERS.length).setValues([[
      new Date(), clean_(d.name), user, clean_(d.date), clean_(d.room), clean_(d.time),
      clean_(d.result), clean_(d.details).substring(0, 500), clean_(d.source),
    ]]);
    if (s.getLastRow() > 1001) s.deleteRows(1002, s.getLastRow() - 1001); // keep the newest 1000
  } finally {
    lock.releaseLock();
  }
  if (/^Booked/.test(clean_(d.result))) {
    try { refreshBookings(); } catch (err) { /* the page catches up at the nightly refresh */ }
  }
  return reply_('ok');
}

/** True if the username appears in the Username column of any tab that has one. */
function inPlan_(user) {
  var sheets = SpreadsheetApp.getActive().getSheets();
  for (var i = 0; i < sheets.length; i++) {
    var name = sheets[i].getName();
    if (name === STATUS || name === GUIDE || name === BOOKINGS) continue;
    var values = sheets[i].getDataRange().getDisplayValues();
    var col = -1;
    for (var r = 0; r < values.length; r++) {
      if (col < 0) {
        col = values[r].map(function (v) { return String(v).trim().toLowerCase(); }).indexOf('username');
        continue;
      }
      if (String(values[r][col]).trim().split('@')[0].toLowerCase() === user) return true;
    }
  }
  return false;
}

/** Plain text only: a value starting with = + - @ would otherwise run as a formula. */
function clean_(v) {
  var t = String(v == null ? '' : v).replace(/[\r\n]+/g, ' ').trim().substring(0, 500);
  return /^[=+\-@]/.test(t) ? "'" + t : t;
}

function reply_(text) {
  return ContentService.createTextOutput(text);
}

/* ------------------------------------------------------------------------------------------ */
/* Room bookings: the first tab, for everyone to look at.                                      */
/* ------------------------------------------------------------------------------------------ */

var INK = '#1f2937', MUTED = '#6b7280', FAINT = '#9ca3af', LINE = '#e5e7eb', ACCENT = '#b4122e';
// Soft colour pairs (background, text), one per person, picked from the name so it never changes.
var PEOPLE_COLOURS = [['#e8f0fe', '#1a4fb4'], ['#e6f4ea', '#146c2e'], ['#fff1e0', '#9a4b00'], ['#f3e8fd', '#6f2da8'],
                      ['#fde7ec', '#a61b3f'], ['#e0f5f3', '#0f6b62'], ['#fdf6d8', '#7a5a00'], ['#ecebfd', '#3f3aa8']];

/** Every booking the booker made (Booked rows on the Status tab), one per date, room and time. */
function bookings_() {
  var s = SpreadsheetApp.getActive().getSheetByName(STATUS);
  if (!s || s.getLastRow() < 2) return [];
  var rows = s.getRange(2, 1, s.getLastRow() - 1, HEADERS.length).getValues();
  var seen = {}, out = [];
  rows.forEach(function (r) {
    if (!/^Booked/.test(String(r[6]))) return;
    var date = r[3] instanceof Date ? Utilities.formatDate(r[3], TZ, 'yyyy-MM-dd') : String(r[3]).trim();
    var time = String(r[5]).trim(), room = String(r[4]).trim();
    var m = time.match(/^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$/);
    if (!/^\d{4}-\d{2}-\d{2}$/.test(date) || !m || !room) return;
    var key = date + '|' + room + '|' + time;
    if (seen[key]) return;
    seen[key] = true;
    out.push({ date: date, start: +m[1] * 60 + +m[2], end: +m[3] * 60 + +m[4], room: room,
               who: String(r[1]).trim() || String(r[2]).trim() });
  });
  out.sort(function (a, b) { return a.date < b.date ? -1 : a.date > b.date ? 1 : a.start - b.start || (a.room < b.room ? -1 : 1); });
  return out;
}

function ymd_(d) { return Utilities.formatDate(d, 'UTC', 'yyyy-MM-dd'); }
function utc_(s) { var p = s.split('-'); return new Date(Date.UTC(+p[0], +p[1] - 1, +p[2])); }
function addDays_(s, n) { var d = utc_(s); d.setUTCDate(d.getUTCDate() + n); return ymd_(d); }
function dayLabel_(s) { return Utilities.formatDate(utc_(s), 'UTC', 'EEE MMM d'); }
function clock_(m) {
  var h = Math.floor(m / 60), mm = m % 60, ap = h < 12 ? 'AM' : 'PM', h12 = (h + 11) % 12 + 1;
  return h12 + (mm ? ':' + ('0' + mm).slice(-2) : '') + ' ' + ap;
}
function span_(b) { return clock_(b.start) + ' – ' + clock_(b.end); }
function colour_(name) {
  var h = 0;
  for (var i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) % 9973;
  return PEOPLE_COLOURS[h % PEOPLE_COLOURS.length];
}

/** Rebuilds the Room bookings tab (and puts it first). Safe to run any time. */
function refreshBookings() {
  var ss = SpreadsheetApp.getActive();
  var sh = ss.getSheetByName(BOOKINGS);
  if (!sh) sh = ss.insertSheet(BOOKINGS, 0);
  if (sh.getIndex() !== 1) { ss.setActiveSheet(sh); ss.moveActiveSheet(1); }

  var all = bookings_();
  var today = Utilities.formatDate(new Date(), TZ, 'yyyy-MM-dd');
  var monday = addDays_(today, -((utc_(today).getUTCDay() + 6) % 7));
  var W = 5; // columns: margin, when, room, who, margin

  // Each output row: values plus its look. Built first, then written in one go.
  var rows = [];
  function row(vals, look) { rows.push({ v: vals, k: look || {} }); }
  function blank(h) { row(['', '', '', '', ''], { h: h || 14 }); }

  row(['', 'Canal Building study rooms', '', '', ''], { title: true, h: 44 });
  row(['', 'Booked by Canal Booker for the team · updated ' +
       Utilities.formatDate(new Date(), TZ, 'EEE MMM d, h:mm a'), '', '', ''], { sub: true, h: 22 });
  blank(18);

  function week(title, start) {
    var end = addDays_(start, 6);
    row(['', title, '', '', dayLabel_(start).slice(4) + ' – ' + dayLabel_(end).slice(4)], { section: true, h: 34 });
    row(['', 'TIME', 'ROOM', 'WHO', ''], { labels: true, h: 22 });
    for (var i = 0; i < 7; i++) {
      var day = addDays_(start, i);
      var those = all.filter(function (b) { return b.date === day; });
      if (i >= 5 && !those.length) continue; // weekends only when something is booked
      var past = day < today;
      row(['', dayLabel_(day), '', '', day === today ? 'TODAY' : ''], { day: true, today: day === today, past: past, h: 30 });
      if (!those.length) row(['', 'No bookings', '', '', ''], { none: true, h: 24 });
      those.forEach(function (b) {
        row(['', span_(b), b.room, b.who, ''], { booking: true, past: past, who: b.who, h: 26 });
      });
    }
    blank(22);
  }
  week('This week', monday);
  week('Next week', addDays_(monday, 7));

  var upcoming = all.filter(function (b) { return b.date >= today; });
  var earlier = all.filter(function (b) { return b.date < today; }).reverse().slice(0, 30);
  row(['', 'All bookings', '', '', upcoming.length + ' coming up'], { section: true, h: 34 });
  row(['', 'DATE · TIME', 'ROOM', 'WHO', ''], { labels: true, h: 22 });
  if (!upcoming.length) row(['', 'Nothing booked yet', '', '', ''], { none: true, h: 24 });
  upcoming.forEach(function (b) {
    row(['', dayLabel_(b.date) + '  ·  ' + span_(b), b.room, b.who, ''], { booking: true, who: b.who, h: 26 });
  });
  if (earlier.length) {
    blank(10);
    row(['', 'EARLIER', '', '', ''], { labels: true, h: 22 });
    earlier.forEach(function (b) {
      row(['', dayLabel_(b.date) + '  ·  ' + span_(b), b.room, b.who, ''], { booking: true, past: true, who: b.who, h: 24 });
    });
  }
  blank(18);
  row(['', 'Only bookings made by the booker show here. Your own list: booking.carleton.ca > My Bookings.', '', '', ''],
      { foot: true, h: 22 });

  // Write values and looks.
  sh.clear();
  sh.getBandings().forEach(function (b) { b.remove(); });
  var n = rows.length;
  if (sh.getMaxRows() < n + 2) sh.insertRowsAfter(sh.getMaxRows(), n + 2 - sh.getMaxRows());
  if (sh.getMaxRows() > n + 20) sh.deleteRows(n + 21, sh.getMaxRows() - n - 20);
  if (sh.getMaxColumns() > W) sh.deleteColumns(W + 1, sh.getMaxColumns() - W);
  var range = sh.getRange(1, 1, n, W);
  var bg = [], fc = [], fw = [], fs = [], fst = [], al = [];
  rows.forEach(function (r) {
    var k = r.k, b = ['#ffffff', '#ffffff', '#ffffff', '#ffffff', '#ffffff'], c = [INK, INK, INK, INK, MUTED];
    var w = ['normal', 'normal', 'normal', 'normal', 'normal'], z = [10, 11, 11, 11, 10];
    var st = ['normal', 'normal', 'normal', 'normal', 'normal'], a = ['left', 'left', 'left', 'left', 'right'];
    if (k.title) { z[1] = 22; w[1] = 'bold'; }
    if (k.sub) { c[1] = MUTED; z[1] = 10; }
    if (k.section) { z[1] = 15; w[1] = 'bold'; c[1] = ACCENT; z[4] = 10; }
    if (k.labels) { c = [FAINT, FAINT, FAINT, FAINT, FAINT]; z = [9, 9, 9, 9, 9]; w = ['bold', 'bold', 'bold', 'bold', 'bold']; }
    if (k.day) {
      b = ['#ffffff', '#f6f7f9', '#f6f7f9', '#f6f7f9', '#f6f7f9']; w[1] = 'bold';
      if (k.today) { b = ['#ffffff', '#fdecef', '#fdecef', '#fdecef', '#fdecef']; c[1] = ACCENT; c[4] = ACCENT; w[4] = 'bold'; z[4] = 9; }
      if (k.past) c[1] = FAINT;
    }
    if (k.none) { c[1] = FAINT; st[1] = 'italic'; z[1] = 10; }
    if (k.booking) {
      var pc = colour_(k.who || '');
      b[3] = pc[0]; c[3] = pc[1]; w[3] = 'bold'; c[2] = MUTED;
      if (k.past) { c = [FAINT, FAINT, FAINT, FAINT, FAINT]; b[3] = '#f6f7f9'; w[3] = 'normal'; }
    }
    if (k.foot) { c[1] = FAINT; z[1] = 9; st[1] = 'italic'; }
    bg.push(b); fc.push(c); fw.push(w); fs.push(z); fst.push(st); al.push(a);
  });
  range.setValues(rows.map(function (r) { return r.v; }))
    .setBackgrounds(bg).setFontColors(fc).setFontWeights(fw).setFontSizes(fs).setFontStyles(fst)
    .setHorizontalAlignments(al).setVerticalAlignment('middle').setFontFamily('Roboto').setWrap(false);
  rows.forEach(function (r, i) {
    sh.setRowHeight(i + 1, r.k.h || 24);
    if (r.k.section) sh.getRange(i + 1, 2, 1, W - 1).setBorder(null, null, true, null, null, null, ACCENT, SpreadsheetApp.BorderStyle.SOLID_MEDIUM);
    if (r.k.booking) sh.getRange(i + 1, 2, 1, W - 1).setBorder(null, null, true, null, null, null, LINE, SpreadsheetApp.BorderStyle.SOLID);
  });
  [28, 230, 110, 150, 120].forEach(function (px, j) { sh.setColumnWidth(j + 1, px); });
  sh.setHiddenGridlines(true);
  sh.setFrozenRows(0);
  sh.setTabColor(ACCENT);
  protect_(sh);
}

/** Only the sheet owner (and this script) can change the tab; everyone else can still look at it. */
function protect_(sh) {
  if (!sh) return;
  var p = sh.getProtections(SpreadsheetApp.ProtectionType.SHEET)[0] || sh.protect();
  p.setDescription('Filled in by Canal Booker');
  p.removeEditors(p.getEditors().filter(function (u) { return u.getEmail() !== Session.getEffectiveUser().getEmail(); }));
  if (p.canDomainEdit()) p.setDomainEdit(false);
}
