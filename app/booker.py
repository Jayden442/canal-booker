"""Drives the Carleton booking portal in a real browser.

The clicks and typing are not hard coded here. They come from
portal_recipe.json, a list of simple steps. If the portal changes, only
the recipe needs editing, and a fixed recipe on GitHub reaches everyone.

Step format:
  {"action": "goto",   "url": "..."}
  {"action": "click",  "selector": "..."}
  {"action": "fill",   "selector": "...", "value": "{room}"}
  {"action": "select", "selector": "...", "value": "..."}      (dropdown, by visible label;
                                                                add "by": "value" to match the option value)
  {"action": "press",  "selector": "...", "key": "Enter"}
  {"action": "check",  "selector": "..."}
  {"action": "expect", "selector": "..."}                      (wait until it shows up)
  {"action": "wait",   "ms": 1000}
Optional keys: "optional": true, "timeout": ms, "note": "...",
  "on_fail": "unavailable" | "login_failed" | "error",
  "submit": true  (a dry run stops right before this step),
  "race_start": true  (midnight mode: steps before this are done early, steps from here on
                       run once the "race.ready" selector shows up after "race.refresh").
Placeholders: {username} {password} {room} {date} {start} {end} {title} {attendees} {day}
  {room_number} (last word of the room, "CB 2103" -> "2103")
  {date_epoch} (the date as UTC midnight in seconds, used by the portal's calendar cells)
  {start_min} {end_min} {duration_min} (minutes after midnight / length in minutes)
Selectors are Playwright selectors: CSS, text=..., role=button[name="Search"], etc.
"""
import calendar
import datetime as dt
import email.utils
import re
import time
import urllib.parse

from . import storage


class StepFailed(Exception):
    def __init__(self, outcome, index, step, detail):
        self.outcome = outcome
        self.index = index
        self.step = step
        self.detail = detail
        super().__init__(detail)


def _render(value, ctx):
    if not isinstance(value, str):
        return value
    for k, v in ctx.items():
        value = value.replace("{" + k + "}", str(v))
    return value


def _scrub(text, secret):
    text = str(text).splitlines()[0][:300] if text else ""
    return text.replace(secret, "********") if secret else text


# Tabs that are not in front are slowed down by the browser (timers and animation frames), which
# made every click in them take a second or two. Midnight mode keeps up to 6 tabs open, so turn that off.
# Seconds around the open time at which the tabs ask again. A tab that skips the calendar asks at
# each of these moments; calendar tabs take one each, in turn (the first choice right on time).
STAGGER = (0.0, -0.1, 0.1)
BROWSER_ARGS = ["--disable-background-timer-throttling", "--disable-renderer-backgrounding",
                "--disable-backgrounding-occluded-windows"]


def _launch(p, show):
    """Use Edge or Chrome already on the computer, so nothing extra has to be downloaded."""
    last = None
    for channel in ("msedge", "chrome", None):
        try:
            if channel:
                return p.chromium.launch(channel=channel, headless=not show, args=BROWSER_ARGS)
            return p.chromium.launch(headless=not show, args=BROWSER_ARGS)
        except Exception as e:
            last = e
    raise RuntimeError("No browser found. Please install Google Chrome or Microsoft Edge.") from last


def _settle(page):
    """Wait for the portal to finish reloading the form after a click or dropdown change."""
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass


def run_steps(page, steps, ctx, dry_run=False, settle=False, timings=None):
    """settle=True waits for the page to go quiet after each click or dropdown change. Slower,
    so it is used for the early preparation, not for the steps raced at the open time."""
    for i, step in enumerate(steps):
        if step.get("submit") and dry_run:
            return "dry_run"
        action = step.get("action")
        timeout = step.get("timeout", 10000)
        started = time.time()
        try:
            if action == "goto":
                page.goto(_render(step["url"], ctx), wait_until="domcontentloaded", timeout=30000)
            elif action == "wait":
                page.wait_for_timeout(step.get("ms", 1000))
            else:
                loc = page.locator(_render(step["selector"], ctx)).first
                if action == "click":
                    loc.click(timeout=timeout)
                elif action == "fill":
                    loc.fill(str(_render(step.get("value", ""), ctx)), timeout=timeout)
                elif action == "select":
                    value = str(_render(step.get("value", ""), ctx))
                    if step.get("by") == "value":
                        loc.select_option(value=value, timeout=timeout)
                    else:
                        loc.select_option(label=value, timeout=timeout)
                elif action == "press":
                    loc.press(step.get("key", "Enter"), timeout=timeout)
                elif action == "check":
                    loc.check(timeout=timeout)
                elif action == "expect":
                    loc.wait_for(state="visible", timeout=timeout)
                else:
                    raise ValueError(f"Unknown step action '{action}'")
                if settle and action in ("click", "select", "press", "check"):
                    _settle(page)
            if timings is not None:
                timings.append((step.get("note") or action, time.time() - started))
        except Exception as e:
            if timings is not None:
                timings.append(((step.get("note") or action) + " (failed)", time.time() - started))
            if step.get("optional"):
                continue
            raise StepFailed(step.get("on_fail", "error"), i, step, e)
    return "done"


def _timing_text(timings):
    """'Pick the date 0.31 s, Wait for the list ... 0.52 s' with notes cut short."""
    return ", ".join(f"{n.split(' (')[0][:32]} {s:.2f} s" for n, s in timings)


_MARK = "() => document.querySelectorAll(\"[id^='day']\").forEach(e => e.setAttribute('data-cb-old', '1'))"
# Still reloading while the marked old days are on the page, or while no days are shown yet (the
# portal clears the calendar the moment Verify Calendar is clicked and fills it when the answer arrives).
_RELOADING = "() => !!document.querySelector('[data-cb-old]') || !document.querySelector(\"[id^='day']\")"


def _reloading(page):
    """True while a reload started by _fire is still on its way: the marked calendar days are still
    on the page, no new days are shown yet, or the page is between documents."""
    try:
        return page.evaluate(_RELOADING)
    except Exception:
        return True


def _fire(page, steps, ctx):
    """Start the race "refresh" steps (Verify Calendar) without waiting for the page to reload, so
    every tab reloads at the same time. Clicks go through the page's own JavaScript; anything that
    is not a simple click falls back to the normal step runner. The calendar's day cells are marked
    first: the reload is done when the marked cells are gone (replaced or a new page)."""
    try:
        page.evaluate(_MARK)
    except Exception:
        pass
    for step in steps:
        if step.get("action") == "click":
            try:
                page.evaluate("sel => { const e = document.querySelector(sel); if (e) e.click(); }",
                              _render(step["selector"], ctx))
                continue
            except Exception as e:
                if "context was destroyed" in str(e) or "navigat" in str(e).lower():
                    continue  # the click already started a page load
        try:
            run_steps(page, [step], ctx)
        except StepFailed:
            pass


def _measure_reload(page, steps, ctx, limit=5.0):
    """Seconds one calendar reload takes on the portal (fire, then wait until it is replaced)."""
    started = time.time()
    _fire(page, steps, ctx)
    while _reloading(page) and time.time() - started < limit:
        page.wait_for_timeout(20)
    return time.time() - started


# Skipping the calendar (recipe race.direct): each tab asks for its start-time list straight away,
# the same request the portal makes when a date is picked, and books from the answer.
_DIRECT_FIRE = """([u, f, marker, secs]) => {
  const s = window.__cbList = window.__cbList || {pending: 0, state: 'none', odd: 0};
  const read = t => {
    if (!t || !t.includes(marker)) return 'odd';
    const d = new DOMParser().parseFromString(t, 'text/html');
    for (const div of d.querySelectorAll('.listDiv')) {
      let data; try { data = JSON.parse(div.getAttribute('data-listData')); } catch (e) { continue; }
      for (const row of (data.RowData || [])) {
        const cells = row.rowData || [];
        if (Array.isArray(cells[0]) && Number(cells[0][1]) === secs && String(cells[1]).includes('Book')) {
          const box = document.createElement('div'); box.id = 'cb-direct'; box.innerHTML = cells[1];
          // On top of everything, so the click lands at once (the portal's own layout can cover the end of the page).
          box.style.cssText = 'position:fixed;top:0;left:0;z-index:2147483647;background:#fff;padding:4px';
          if (box.querySelector('input')) { document.body.appendChild(box); return 'ready'; }
        }
      }
    }
    return 'closed';
  };
  s.pending++;
  fetch(u, {method: 'POST', body: f, credentials: 'include', headers: {
      'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8', 'X-Requested-With': 'XMLHttpRequest'}})
    .then(r => r.text()).catch(() => '').then(t => {
      s.pending--;
      if (s.state === 'ready') return;
      const state = read(t);
      s.odd = state === 'odd' ? s.odd + 1 : 0;
      if (state !== 'odd' || s.odd >= 3) s.state = state;
    }); }"""
# The state of the answers so far: 'ready' (the Book button for this start time is on the page, in
# #cb-direct), 'pending' (no answer yet), 'closed' (no such start time yet), 'odd' (three answers in
# a row that were not a start-time list) or 'none'. Also how many requests are still on their way.
_DIRECT_STATE = """() => { const s = window.__cbList;
  if (document.querySelector('#cb-direct input')) return ['ready', 0];
  if (!s) return ['none', 0];
  return [s.state === 'none' && s.pending ? 'pending' : s.state, s.pending]; }"""


def _direct_state(page):
    """(state, requests still on their way); see _DIRECT_STATE."""
    try:
        return tuple(page.evaluate(_DIRECT_STATE))
    except Exception:  # the page is between documents
        return "pending", 1


def _direct_fire(page, direct, ctx):
    """Ask for the start-time list without waiting for the answer. Unlike a calendar reload, a new
    request does not cancel the last one, so several can be on their way at once."""
    try:
        page.evaluate(_DIRECT_FIRE, [direct["url"], _render(direct["form"], ctx),
                                     direct.get("answer_marker", ""), ctx["start_min"] * 60])
    except Exception:
        pass


def _capture(direct, ctx):
    """A request listener that copies the fields named in direct.capture (the room's id and the
    booking type's id) from the portal's calendar request into ctx."""
    cap = direct.get("capture") or {}

    def on_request(req):
        try:
            if cap.get("url") and cap["url"] in req.url and req.post_data:
                form = urllib.parse.parse_qs(req.post_data, keep_blank_values=True)
                for key, field in (cap.get("fields") or {}).items():
                    if form.get(field) and form[field][0]:
                        ctx[key] = form[field][0]
        except Exception:
            pass
    return on_request


def _step_label(err: StepFailed):
    return err.step.get("note") or f"{err.step.get('action')} {err.step.get('selector', err.step.get('url', ''))}"


def _shot(page, name):
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", name)[:120] + ".png"
    try:
        page.screenshot(path=str(storage.data_dir() / "screenshots" / safe), full_page=True)
        return safe
    except Exception:
        return None


def _login(page, recipe, ctx, password):
    try:
        run_steps(page, recipe.get("login", []), ctx)
        return True, "Signed in."
    except StepFailed as e:
        if e.outcome == "login_failed":
            return False, "Carleton did not accept the sign-in. Check your username and password."
        return False, f"Sign-in step failed at: {_step_label(e)} ({_scrub(e.detail, password)})"


def _base_ctx(settings, password):
    return {
        "username": settings["username"],
        "password": password,
        "title": settings.get("event_title", "Group study"),
        "attendees": settings.get("attendees", 4),
    }


def test_login(settings, password):
    from playwright.sync_api import sync_playwright
    recipe = storage.load_recipe()
    with sync_playwright() as p:
        browser = _launch(p, settings.get("show_browser"))
        page = browser.new_page()
        ok, msg = _login(page, recipe, _base_ctx(settings, password), password)
        shot = _shot(page, f"login_test_{dt.datetime.now():%Y%m%d_%H%M%S}")
        browser.close()
    return {"ok": ok, "message": msg, "screenshot": shot}


def _ctx(base, recipe, day, room, start_text, end_text):
    date_fmt = recipe.get("date_format", "%Y-%m-%d")
    time_fmt = recipe.get("time_format", "%H:%M")
    start = dt.datetime.strptime(start_text, "%H:%M")
    end = dt.datetime.strptime(end_text, "%H:%M")

    def fmt(t):
        text = t.strftime(time_fmt)
        return text.lstrip("0") if recipe.get("strip_leading_zero") else text

    return dict(base, room=room, date=day.strftime(date_fmt), day=storage.DAYS[day.weekday()],
                room_number=room.split()[-1] if room.split() else room,
                date_epoch=calendar.timegm(day.timetuple()),
                start_min=start.hour * 60 + start.minute, end_min=end.hour * 60 + end.minute,
                duration_min=int((end - start).total_seconds() // 60),
                start=fmt(start), end=fmt(end))


def _clock_offset(page, url):
    """Seconds the portal's clock is ahead of ours.

    The Date header only has whole seconds, so one reading is up to half a second off. Instead,
    ask about every 0.1 s until the portal's second ticks over: the tick happened between the two
    readings, which pins the offset down to roughly 0.1 s plus half the round trip."""
    def read():
        before = time.time()
        resp = page.request.head(url, timeout=10000)
        after = time.time()
        return (before + after) / 2, email.utils.parsedate_to_datetime(resp.headers["date"]).timestamp()
    try:
        prev_mid, prev_s = read()
        for _ in range(25):
            time.sleep(0.1)
            mid, s = read()
            if s > prev_s:  # the second ticked between the previous reading and this one
                return s - (prev_mid + mid) / 2
            prev_mid, prev_s = mid, s
        return prev_s + 0.5 - prev_mid  # no tick seen: fall back to the middle of the second
    except Exception:
        return 0.0


MAX_RACE_TABS = 6
PREPARE_TRIES = 3


def race_booking(settings, password, recipe, day, slot, open_at, dry_run=False):
    """Midnight mode for one day that is about to open.

    Signs in early and opens one tab per room and time choice, each filled in up to the
    calendar. From just before open_at (by the portal's clock) every tab re-checks the
    calendar together, and the first choice that shows the day as free is booked.
    """
    from playwright.sync_api import sync_playwright
    steps = recipe.get("book", [])
    cut = next((i for i, s in enumerate(steps) if s.get("race_start")), None)
    race = recipe.get("race") or {}
    result = {"date": day.isoformat(), "slot": storage.slot_key(slot), "room": None, "race": True,
              "status": "unavailable", "message": "Midnight mode: none of your choices opened up."}
    if cut is None or not race.get("ready"):
        result.update(status="error", message="This version of the booking steps has no midnight mode.")
        return result
    prepare, grab = steps[:cut], steps[cut:]
    # Skipping the calendar: book with direct.grab, then the normal steps from direct.then on.
    direct = race.get("direct") or {}
    then = next((i for i, s in enumerate(grab) if direct.get("then") and
                 (s.get("note") or "").startswith(direct["then"])), None)
    direct_grab = (direct.get("grab", []) + grab[then:]) if then is not None and direct.get("url") else []
    times = slot.get("times") or storage.slot_times(slot)
    rooms = slot.get("rooms") or settings["rooms"]  # that day's rooms from the plan, else the person's
    options = [(r, t) for r in rooms for t in times][:MAX_RACE_TABS]
    poll = int(settings.get("race_poll_ms", 1500)) / 1000
    window = int(settings.get("race_window_seconds", 120))
    base = _base_ctx(settings, password)
    notes = []

    with sync_playwright() as p:
        browser = _launch(p, settings.get("show_browser"))
        context = browser.new_context()
        first = context.new_page()
        ok, msg = _login(first, recipe, base, password)
        if not ok:
            result.update(status="login_failed", message=msg, screenshot=_shot(first, "race_login_failed"))
            browser.close()
            return result
        offset = _clock_offset(first, race.get("clock_url", "https://booking.carleton.ca/"))

        open_local = open_at.timestamp() - offset
        tabs = []
        for i, (room, (start_text, end_text)) in enumerate(options):
            page = first if i == 0 else context.new_page()
            ctx = _ctx(base, recipe, day, room, start_text, end_text)
            label = f"{room} {start_text}-{end_text}"
            listener = _capture(direct, ctx)
            page.on("request", listener)
            for attempt in range(PREPARE_TRIES):
                try:
                    run_steps(page, prepare, ctx, settle=True)
                    tabs.append({"page": page, "ctx": ctx, "label": label, "room": room,
                                 "time": f"{start_text}-{end_text}", "alive": True})
                    break
                except StepFailed as e:
                    # Start the tab over from the first page, unless the open time is under
                    # 30 s away (a test run starts after the open time, so it always retries).
                    if attempt + 1 < PREPARE_TRIES and not (open_local - 30 <= time.time() < open_local):
                        continue
                    notes.append(f"{label} could not get ready at '{_step_label(e)}' "
                                 f"({_scrub(e.detail, password)})")
                    _shot(page, f"{day}_race_prepare_{room}_{start_text}")
        if not tabs:
            result.update(status="error", message="Midnight mode: no choice got ready. " + "; ".join(notes),
                          screenshot=_shot(first, f"{day}_race_prepare_failed"))
            browser.close()
            return result

        # Wait on the page until 3 seconds before the portal's clock reaches the open time.
        already_open = time.time() >= open_local
        while time.time() < open_local - 3:
            first.wait_for_timeout(min(1000, max(50, (open_local - 3 - time.time()) * 1000)))

        def _when():
            after = time.time() + offset - open_at.timestamp()
            return "(the day was already open)" if already_open else f"{max(after, 0):.1f} s after opening"

        deadline = max(open_local, time.time()) + window
        refresh = race.get("refresh", [])
        ready_sel = race["ready"]
        # How long one calendar reload takes on the portal (the last preparation step, measured above).
        # Near the open time each waiting tab reloads again as soon as the last reload could be back.
        reload_s = _measure_reload(tabs[0]["page"], refresh, tabs[0]["ctx"])
        rapid = max(0.3, reload_s)
        # First reload now (3 s early). The reload right at the open time is spread across the tabs
        # (0.1 s early, on time, 0.1 s late, and so on) so a small clock error doesn't cost a full
        # extra reload: whichever tab lands first just after the opening sees the day.
        # A tab skips the calendar once its calendar request has given the room's ids. Playwright
        # passes on request events only while it is busy, so give them up to a second to arrive.
        fields = list((direct.get("capture") or {}).get("fields", {}))
        captured = lambda t: bool(direct_grab) and bool(fields) and all(k in t["ctx"] for k in fields)
        wait_until = time.time() + 1
        while direct_grab and not all(captured(t) for t in tabs) and time.time() < wait_until:
            first.wait_for_timeout(50)
        switched = []  # tabs whose direct answer looked wrong and went back to the calendar
        for t in tabs:
            t["next"] = time.time()
            t["state"] = "none"
            t["direct"] = captured(t)
        sync = f"Portal clock {offset:+.2f} s from ours; calendar reload {reload_s:.2f} s."

        def is_ready(t):
            if t["direct"]:
                state, t["pending"] = _direct_state(t["page"])
                t["state"] = state
                if state == "odd":  # not a start-time list: use the calendar for this tab from now on
                    t["direct"] = False
                    switched.append(t["label"])
                return state == "ready"
            try:
                return t["page"].locator(_render(ready_sel, t["ctx"])).count() > 0
            except Exception:  # the page is mid-reload
                return False

        def direct_note():
            used = sum(1 for t in tabs if t["direct"])
            text = (f" Start-time list asked for directly (calendar skipped) on {used} of {len(tabs)} tabs."
                    if used else " Calendar used." if switched
                    else " Calendar used (the direct start-time list was not set up).")
            if switched:
                text += f" Went back to the calendar for: {', '.join(switched)}."
            return text

        def busy(t):
            # Direct requests don't cancel each other: up to 3 may be on their way at once.
            return t.get("pending", 0) >= 3 if t["direct"] else _reloading(t["page"])

        def fire(t, now):
            # Never ask again while this tab's last request is still on its way (that would cancel
            # a calendar reload), unless it has been stuck for 2.5 s. Returns True if it asked.
            if t["alive"] and not is_ready(t) and (not busy(t) or now - t.get("fired", 0) > 2.5):
                if t["direct"]:
                    _direct_fire(t["page"], direct, t["ctx"])
                    t["pending"] = t.get("pending", 0) + 1
                else:
                    _fire(t["page"], refresh, t["ctx"])
                t["fired"] = now
                return True
            return False

        while time.time() < deadline and any(t["alive"] for t in tabs):
            now = time.time()
            for i, t in enumerate(tabs):
                if now < t["next"]:
                    continue
                if not fire(t, now) and now >= open_local - 0.5:
                    t["next"] = now + 0.02  # still waiting on the last answer: ask again once it is back
                    continue
                if now < open_local - 0.5:
                    # Direct tabs ask at every moment around the open time; a calendar tab can only
                    # reload once at a time, so the moments are spread across the calendar tabs.
                    offsets = sorted(STAGGER) if t["direct"] else [STAGGER[i % len(STAGGER)]]
                    t["plan"] = [open_local + o for o in offsets]
                    t["next"] = t["plan"].pop(0)
                elif t.get("plan"):
                    t["next"] = t["plan"].pop(0)
                elif now < open_local + 8:
                    t["next"] = now + rapid  # the first seconds after opening: reload again quickly
                else:
                    t["next"] = now + poll
            ready = [t for t in tabs if t["alive"] and is_ready(t)]
            if not ready:
                first.wait_for_timeout(100 if min(t["next"] for t in tabs) - time.time() > 0.1 else 20)
                continue
            # The day is open: every other tab still showing the old calendar reloads right away.
            for o in tabs:
                if o not in ready:
                    fire(o, time.time())
            if ready[0] is not next(t for t in tabs if t["alive"]):
                # A lower choice opened first; give better choices still reloading a moment to catch up.
                first.wait_for_timeout(250)
                ready = [t for t in tabs if t["alive"] and is_ready(t)] or ready
            t = ready[0]  # the best choice that is open right now
            try:
                t["page"].bring_to_front()  # a tab in front is not slowed down by the browser
                steps_timed = []
                outcome = run_steps(t["page"], direct_grab if t["direct"] else grab, t["ctx"],
                                    dry_run=dry_run, timings=steps_timed)
                when = _when()
                shot = _shot(t["page"], f"{day}_race_{t['room']}_{outcome}")
                # Say what happened to the choices tried before this one (taken by someone else, and when).
                before = f" Before that: {'; '.join(notes)}." if notes else ""
                timing = f" {sync}{direct_note()} Step times: {_timing_text(steps_timed)}."
                if outcome == "dry_run":
                    result.update(room=t["room"], time=t["time"], status="dry_run", screenshot=shot,
                                  message=f"Midnight mode dry run: {t['label']} was free {when}. Nothing was booked.{before}{timing}")
                else:
                    result.update(room=t["room"], time=t["time"], status="booked", screenshot=shot,
                                  message=f"Midnight mode booked {t['label']} {when}.{before}{timing}")
                browser.close()
                return result
            except StepFailed as e:
                # No screenshot here: it costs about a second, and the next choice is waiting.
                t["alive"] = False
                t["failed"] = e.outcome
                notes.append(f"{t['label']} " + (f"was taken by someone else (seen {_when()})"
                                                 if e.outcome == "unavailable"
                                                 else f"failed at '{_step_label(e)}' {_when()}"))
        if notes:
            result["message"] += " " + "; ".join(notes) + "."
        last = "; ".join(f"{t['label']}: {t['state']}" for t in tabs if t["direct"])
        result["message"] += (" " + _calendar_seen(tabs, day) + " " + sync + direct_note()
                              + (f" Last direct answers: {last}." if last else ""))
        result["screenshot"] = _shot(tabs[0]["page"], f"{day}_race_nothing_booked")
        browser.close()
    return result


def _calendar_seen(tabs, day):
    """What the first choice's calendar shows, to tell "this day is full or closed" apart from
    "the bot can't see any open day" (the page changed)."""
    for t in tabs:
        try:
            page = t["page"]
            shown = page.eval_on_selector_all("[id^='day']", "els => els.map(e => e.id)")
            open_ids = page.eval_on_selector_all("td.dayAvailable [id^='day']", "els => els.map(e => e.id)")
        except Exception:
            continue
        def dates(ids):
            out = []
            for i in ids:
                try:
                    out.append(dt.datetime.fromtimestamp(int(i[3:]), dt.timezone.utc).date())
                except ValueError:
                    pass
            return sorted(set(out))
        shown, open_days = dates(shown), dates(open_ids)
        if not shown:
            return f"Calendar check ({t['room']}): no calendar days found on the page."
        listed = ", ".join(f"{d:%a %b %d}" for d in open_days[:10]) or "none"
        where = "is in the calendar" if day in shown else "is not in the calendar shown"
        return (f"Calendar check ({t['room']} {t['time']}): {len(shown)} days shown, {day:%b %d} {where}; "
                f"days open for this room and time: {listed}.")
    return "Calendar check: no page to read."


# Requests whose full form and answer the trace logs (the start-time list and the Book form).
TRACE_FULL = ("SelfServiceRoomAvailability", "ConfirmBooking")


def trace_booking(settings, password, recipe, day, slot, probe_day=None):
    """Dry run of one choice that records the portal's traffic after sign-in: every request
    (method, address, form fields) and response (status, type, size, start of the body), with
    the time since the step began. Used to find out which requests the calendar and the
    start-time list really make. Sign-in is not recorded, and headers and cookies never are;
    the username and name are replaced. With probe_day, it then asks for that day's start-time
    list directly (the same request, only the date changed) to see what the portal answers for
    a day that is not open yet. Nothing is booked. Returns (events, outcome text)."""
    from playwright.sync_api import sync_playwright
    steps = recipe.get("book", [])
    times = slot.get("times") or storage.slot_times(slot)
    room = (slot.get("rooms") or settings["rooms"])[0]
    start_text, end_text = times[0]
    base = _base_ctx(settings, password)
    secrets = [x for x in (password, settings.get("username"), settings.get("_name")) if x]

    def clean(text, limit):
        text = " ".join(str(text or "").split())[:limit]
        for s in secrets:
            text = text.replace(s, "<user>" if s != settings.get("_name") else "<name>")
        return text

    events, current, captured = [], {"step": "", "t0": time.time()}, {}

    def on_request(req):
        if req.resource_type in ("image", "font", "stylesheet", "media"):
            return
        full = any(k in req.url for k in TRACE_FULL)
        if full and req.method == "POST" and "SelfServiceRoomAvailability" in req.url:
            captured["list"] = (req.url, req.post_data)
        events.append(f"[{current['step'][:28]:28}] +{time.time() - current['t0']:5.2f}s  -> {req.method} "
                      f"{clean(req.url, 160)}" + (f"  form: {clean(req.post_data, 3000 if full else 300)}"
                                                  if req.post_data else ""))

    def on_response(resp):
        req = resp.request
        if req.resource_type in ("image", "font", "stylesheet", "media"):
            return
        try:
            body = resp.text() if req.resource_type in ("xhr", "fetch", "document") else ""
        except Exception:
            body = ""
        events.append(f"[{current['step'][:28]:28}] +{time.time() - current['t0']:5.2f}s  <- {resp.status} "
                      f"{req.resource_type} {len(body)} chars  {clean(req.url, 100)}"
                      + (f"  body: {clean(body, 8000 if 'SelfServiceRoomAvailability' in req.url else 200)}"
                         if body else ""))

    with sync_playwright() as p:
        browser = _launch(p, settings.get("show_browser"))
        page = browser.new_page()
        ok, msg = _login(page, recipe, base, password)
        if not ok:
            browser.close()
            return events, msg
        page.on("request", on_request)
        page.on("response", on_response)
        ctx = _ctx(base, recipe, day, room, start_text, end_text)
        outcome = "done"
        for step in steps:
            current.update(step=step.get("note") or step.get("action"), t0=time.time())
            try:
                if run_steps(page, [step], ctx, dry_run=True) == "dry_run":
                    outcome = "stopped before the final OK (nothing booked)"
                    break
            except StepFailed as e:
                outcome = f"stopped at '{_step_label(e)}' ({_scrub(e.detail, password)})"
                break
        page.wait_for_timeout(500)
        if probe_day and "list" in captured:
            url, form = captured["list"]
            form = re.sub(r"startDate=\d+", f"startDate={calendar.timegm(probe_day.timetuple())}", form)
            current.update(step=f"Probe: list for {probe_day:%b %d}", t0=time.time())
            try:
                page.evaluate("""([u, f]) => fetch(u, {method: 'POST', body: f, credentials: 'include',
                    headers: {'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                              'X-Requested-With': 'XMLHttpRequest'}}).then(r => r.text())""", [url, form])
                page.wait_for_timeout(300)
            except Exception as e:
                events.append(f"[probe] failed: {clean(e, 200)}")
        direct = (recipe.get("race") or {}).get("direct") or {}
        cut = next((i for i, s in enumerate(steps) if s.get("race_start")), None)
        then = next((i for i, s in enumerate(steps) if direct.get("then") and
                     (s.get("note") or "").startswith(direct["then"])), None)
        if direct.get("url") and cut is not None and then is not None:
            # The same choice again, skipping the calendar the way midnight mode does.
            outcome += " Then skipping the calendar:"
            dctx = _ctx(base, recipe, day, room, start_text, end_text)
            page.on("request", _capture(direct, dctx))
            try:
                for step in steps[:cut]:
                    current.update(step="Direct: " + (step.get("note") or step.get("action")), t0=time.time())
                    run_steps(page, [step], dctx, settle=True)
                page.wait_for_timeout(500)
                missing = [k for k in (direct.get("capture") or {}).get("fields", {}) if k not in dctx]
                if missing:
                    raise StepFailed("error", 0, {"note": "Read the room ids"}, f"not seen: {', '.join(missing)}")
                current.update(step="Direct: ask for the list", t0=time.time())
                _direct_fire(page, direct, dctx)
                state = "pending"
                while state in ("pending", "none") and time.time() - current["t0"] < 5:
                    page.wait_for_timeout(20)
                    state = _direct_state(page)[0]
                events.append(f"[Direct: ask for the list   ] +{time.time() - current['t0']:5.2f}s  answer: {state}")
                outcome += f" list answer '{state}'"
                if state == "ready":
                    for step in direct.get("grab", []) + steps[then:]:
                        current.update(step="Direct: " + (step.get("note") or step.get("action")), t0=time.time())
                        if run_steps(page, [step], dctx, dry_run=True) == "dry_run":
                            outcome += ", stopped before the final OK (nothing booked)"
                            break
                        events.append(f"[{current['step'][:28]:28}] +{time.time() - current['t0']:5.2f}s  step done")
            except StepFailed as e:
                outcome += f" stopped at '{_step_label(e)}' ({_scrub(e.detail, password)})"
            page.wait_for_timeout(500)
        browser.close()
    return events, f"Traced {room} {start_text}-{end_text} on {day:%a %b %d}: {outcome}."


def run_bookings(settings, password, recipe, targets, dry_run=False):
    """targets: list of (date, slot). Returns one result per target."""
    from playwright.sync_api import sync_playwright
    results = []
    base = _base_ctx(settings, password)

    with sync_playwright() as p:
        browser = _launch(p, settings.get("show_browser"))
        page = browser.new_page()
        ok, msg = _login(page, recipe, base, password)
        if not ok:
            shot = _shot(page, "login_failed")
            browser.close()
            return [{"status": "login_failed", "message": msg, "screenshot": shot}]

        for day, slot in targets:
            result = {"date": day.isoformat(), "slot": storage.slot_key(slot), "room": None,
                      "status": "unavailable", "message": "None of your rooms were free."}
            tried = []
            # First room at each time in order, then the next room, and so on.
            times = slot.get("times") or storage.slot_times(slot)
            rooms = slot.get("rooms") or settings["rooms"]
            for room, (start_text, end_text) in [(r, t) for r in rooms for t in times]:
                label = f"{room} {start_text}-{end_text}"
                ctx = _ctx(base, recipe, day, room, start_text, end_text)
                try:
                    outcome = run_steps(page, recipe.get("book", []), ctx, dry_run=dry_run, settle=True)
                    shot = _shot(page, f"{day}_{start_text}_{room}_{outcome}")
                    booked_time = f"{start_text}-{end_text}"
                    if outcome == "dry_run":
                        result.update(room=room, time=booked_time, status="dry_run", screenshot=shot,
                                      message=f"Dry run reached the final button for {label}. Nothing was booked.")
                    else:
                        result.update(room=room, time=booked_time, status="booked", screenshot=shot,
                                      message=f"Booked {label}.")
                    break
                except StepFailed as e:
                    shot = _shot(page, f"{day}_{start_text}_{room}_{e.outcome}")
                    if e.outcome == "unavailable":
                        tried.append(f"{label} taken")
                        result.update(screenshot=shot, message="Taken: " + ", ".join(tried) + ".")
                        continue
                    tried.append(f"{label} failed at '{_step_label(e)}'")
                    result.update(status="error", screenshot=shot,
                                  message="; ".join(tried) + f". Detail: {_scrub(e.detail, password)}")
                    if dry_run:
                        break
            results.append(result)
        browser.close()
    return results
