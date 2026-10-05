/**
 * Canal Booker: Google Sheet helper (Apps Script).
 *
 * Install once (the sheet owner):
 *   1. In the team plan sheet: Extensions > Apps Script. Delete what is there, paste this file, Save.
 *   2. Pick "setup" in the function list at the top and click Run. Allow access when Google asks.
 *      This adds the "Setup guide" and "Status" tabs.
 *   3. Deploy > New deployment > type "Web app". Execute as: Me. Who has access: Anyone. Deploy.
 *      Copy the Web app URL (ends in /exec) and put it in app_defaults.json as "status_url".
 *
 * The booker then posts every result to the Status tab: booked, not booked, sign-in failed,
 * test runs. It only accepts results for usernames that are in the plan tab, and it never
 * receives or stores passwords.
 */

var REPO = 'https://github.com/silix54/canal-booker';
var STATUS = 'Status';
var GUIDE = 'Setup guide';
var HEADERS = ['When (Ottawa)', 'Name', 'Username', 'For date', 'Room', 'Time', 'Result', 'Details', 'Ran from'];

function setup() {
  var ss = SpreadsheetApp.getActive();
  makeGuide_(ss);
  makeStatus_(ss);
  setupDropdowns();
  SpreadsheetApp.getActive().toast('Setup guide, Status tab and dropdowns are ready.');
}

/** Adds a Canal Booker menu to the sheet, so the organizer can redo the dropdowns without opening the script. */
function onOpen() {
  SpreadsheetApp.getUi().createMenu('Canal Booker')
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

/** The tab with the plan: the first one with a row containing Username and Day. */
function findPlan_() {
  var sheets = SpreadsheetApp.getActive().getSheets();
  for (var i = 0; i < sheets.length; i++) {
    var name = sheets[i].getName();
    if (name === STATUS || name === GUIDE) continue;
    var values = sheets[i].getRange(1, 1, Math.min(Math.max(sheets[i].getLastRow(), 1), 30), Math.max(sheets[i].getLastColumn(), 1)).getDisplayValues();
    for (var r = 0; r < values.length; r++) {
      var low = values[r].map(function (v) { return String(v).trim().toLowerCase(); });
      if (low.indexOf('username') >= 0 && low.indexOf('day') >= 0) return { sheet: sheets[i], row: r + 1 };
    }
  }
  return null;
}

function makeGuide_(ss) {
  // After the plan tab, never in front of it: a sheet link without a tab reads the first tab.
  var g = ss.getSheetByName(GUIDE) || ss.insertSheet(GUIDE, 1);
  g.clear();
  var lines = [
    ['# Canal Booker: how to set it up'],
    ['Canal Booker books your Canal Building study room the second it opens (midnight, one week ahead), under your own Carleton account. Your rooms and times come from the plan tab in this sheet.'],
    [''],
    ['# Step 1: add your rows to the plan tab (everyone)'],
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
    [''],
    ['# Step 2, option B: run it on your computer'],
    ['1. Open ' + REPO + '/releases and download CanalBooker.exe (Windows) or CanalBooker-Mac.zip (Mac).'],
    ['2. Open it. Windows: if you see "Windows protected your PC", click More info > Run anyway. Mac: right click > Open the first time.'],
    ['3. Follow the 4 steps at the top of the page that opens: sign in, get your slots from this sheet, dry run, turn on.'],
    ['Keep the computer plugged in, awake and online at midnight. Use option A or option B, not both.'],
    [''],
    ['# Checking if you got your room'],
    ['Open the Status tab. Newest results are at the top: Booked (green), Not booked or Sign-in failed (red), Test passed. The portal\'s My Bookings page always shows what you really have.'],
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
  return reply_('ok');
}

/** True if the username appears in the Username column of any tab that has one. */
function inPlan_(user) {
  var sheets = SpreadsheetApp.getActive().getSheets();
  for (var i = 0; i < sheets.length; i++) {
    var name = sheets[i].getName();
    if (name === STATUS || name === GUIDE) continue;
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
