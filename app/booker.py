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


def _launch(p, show):
    """Use Edge or Chrome already on the computer, so nothing extra has to be downloaded."""
    last = None
    for channel in ("msedge", "chrome", None):
        try:
            if channel:
                return p.chromium.launch(channel=channel, headless=not show)
            return p.chromium.launch(headless=not show)
        except Exception as e:
            last = e
    raise RuntimeError("No browser found. Please install Google Chrome or Microsoft Edge.") from last


def _settle(page):
    """Wait for the portal to finish reloading the form after a click or dropdown change."""
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass


def run_steps(page, steps, ctx, dry_run=False, settle=False):
    """settle=True waits for the page to go quiet after each click or dropdown change. Slower,
    so it is used for the early preparation, not for the steps raced at the open time."""
    for i, step in enumerate(steps):
        if step.get("submit") and dry_run:
            return "dry_run"
        action = step.get("action")
        timeout = step.get("timeout", 10000)
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
        except Exception as e:
            if step.get("optional"):
                continue
            raise StepFailed(step.get("on_fail", "error"), i, step, e)
    return "done"


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
    """Seconds the portal's clock is ahead of ours, from its Date header (about 1 second precise)."""
    try:
        before = time.time()
        resp = page.request.head(url, timeout=10000)
        after = time.time()
        server = email.utils.parsedate_to_datetime(resp.headers["date"]).timestamp()
        return server + 0.5 - (before + after) / 2
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
    times = slot.get("times") or storage.slot_times(slot)
    rooms = slot.get("rooms") or settings["rooms"]  # that day's rooms from the plan, else the person's
    options = [(r, t) for r in rooms for t in times][:MAX_RACE_TABS]
    print(options)
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
        while time.time() < deadline and any(t["alive"] for t in tabs):
            round_start = time.time()
            for t in tabs:
                if t["alive"]:
                    try:
                        run_steps(t["page"], race.get("refresh", []), t["ctx"])
                    except StepFailed:
                        pass
            first.wait_for_timeout(min(700, poll * 1000))
            for t in tabs:  # in order of preference
                if not t["alive"]:
                    continue
                if t["page"].locator(_render(race["ready"], t["ctx"])).count() == 0:
                    continue
                try:
                    outcome = run_steps(t["page"], grab, t["ctx"], dry_run=dry_run)
                    when = _when()
                    shot = _shot(t["page"], f"{day}_race_{t['room']}_{outcome}")
                    # Say what happened to the choices tried before this one (taken by someone else, and when).
                    before = f" Before that: {'; '.join(notes)}." if notes else ""
                    if outcome == "dry_run":
                        result.update(room=t["room"], time=t["time"], status="dry_run", screenshot=shot,
                                      message=f"Midnight mode dry run: {t['label']} was free {when}. Nothing was booked.{before}")
                    else:
                        result.update(room=t["room"], time=t["time"], status="booked", screenshot=shot,
                                      message=f"Midnight mode booked {t['label']} {when}.{before}")
                    browser.close()
                    return result
                except StepFailed as e:
                    t["alive"] = False
                    notes.append(f"{t['label']} " + (f"was taken by someone else (seen {_when()})"
                                                     if e.outcome == "unavailable"
                                                     else f"failed at '{_step_label(e)}' {_when()}"))
                    result["screenshot"] = _shot(t["page"], f"{day}_race_{t['room']}_{e.outcome}")
            left = poll - (time.time() - round_start)
            if left > 0:
                first.wait_for_timeout(left * 1000)
        if notes:
            result["message"] += " " + "; ".join(notes) + "."
        result["message"] += " " + _calendar_seen(tabs, day)
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
