"""The local web page's backend. Only reachable from this computer."""
import os
import re
import secrets
import threading

from flask import Flask, abort, jsonify, request, send_from_directory

from . import autostart, booker, storage

TOKEN = secrets.token_urlsafe(24)
HM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _minutes(t):
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def _check_times(day, start, end):
    if _minutes(end) <= _minutes(start):
        raise ValueError(f"{day}: the end time must be after the start time.")
    if _minutes(end) - _minutes(start) > 180:
        raise ValueError(f"{day}: the portal allows at most 3 hours per person per day.")


def validate(body: dict) -> dict:
    out = {}
    if "username" in body:
        out["username"] = str(body["username"]).strip().split("@")[0]
    if "rooms" in body:
        rooms = [str(r).strip() for r in body["rooms"] if str(r).strip()]
        if len(rooms) > 10:
            raise ValueError("Keep it to 10 rooms or fewer.")
        out["rooms"] = list(dict.fromkeys(rooms))
    if "slots" in body:
        slots, days = [], set()
        for x in body["slots"]:
            day, start, end = x.get("day"), x.get("start", ""), x.get("end", "")
            if day not in storage.DAYS or not HM.match(start) or not HM.match(end):
                raise ValueError("Every time slot needs a day, a start time and an end time.")
            _check_times(day, start, end)
            if day in days:
                raise ValueError(f"{day} appears twice. You can book one slot per day.")
            days.add(day)
            alts = []
            for a in x.get("alts") or []:
                a_start, a_end = str(a.get("start", "")), str(a.get("end", ""))
                if not HM.match(a_start) or not HM.match(a_end):
                    raise ValueError(f"{day}: backup times must look like 09:00-12:00.")
                _check_times(day, a_start, a_end)
                if (a_start, a_end) != (start, end) and {"start": a_start, "end": a_end} not in alts:
                    alts.append({"start": a_start, "end": a_end})
            slot_rooms = [str(r).strip() for r in x.get("rooms") or [] if str(r).strip()]
            slots.append({"day": day, "start": start, "end": end, "alts": alts[:5],
                          "rooms": list(dict.fromkeys(slot_rooms))[:5],  # that day's rooms from the plan
                          "enabled": bool(x.get("enabled", True))})
        out["slots"] = sorted(slots, key=lambda s: storage.DAYS.index(s["day"]))
    if "book_time" in body:
        if not HM.match(str(body["book_time"])):
            raise ValueError("Booking time must look like 00:05.")
        out["book_time"] = body["book_time"]
    if "book_times" in body:
        times = sorted({str(t).strip() for t in body["book_times"] if str(t).strip()})
        if not times:
            raise ValueError("Add at least one time for the booker to run.")
        if any(not HM.match(t) for t in times):
            raise ValueError("Run times must look like 00:00.")
        if len(times) > 12:
            raise ValueError("Keep it to 12 run times a day or fewer.")
        out["book_times"] = times
    if "open_time" in body:
        if not HM.match(str(body["open_time"])):
            raise ValueError("The portal opening time must look like 00:00.")
        out["open_time"] = body["open_time"]
    for key, lo, hi in (("days_ahead", 1, 14), ("retry_minutes", 0, 120),
                        ("retry_every_seconds", 30, 600), ("attendees", 1, 50),
                        ("race_lead_seconds", 60, 900), ("race_window_seconds", 10, 900),
                        ("race_poll_ms", 1000, 10000)):
        if key in body:
            v = int(body[key])
            if not lo <= v <= hi:
                raise ValueError(f"{key.replace('_', ' ')} must be between {lo} and {hi}.")
            out[key] = v
    for key in ("event_title", "team_plan_url", "recipe_url", "status_url"):
        if key in body:
            out[key] = str(body[key]).strip()[:300]
    for key in ("show_browser", "paused", "race"):
        if key in body:
            out[key] = bool(body[key])
    return out


def create_app(scheduler):
    app = Flask(__name__, static_folder=None)
    static_dir = storage.resource_path("app", "static")

    @app.before_request
    def guard():
        host = (request.host or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            abort(403)
        if request.path.startswith("/api/") and request.headers.get("X-Token") != TOKEN:
            abort(403)
        if request.path.startswith("/shots/") and request.args.get("t") != TOKEN:
            abort(403)

    @app.get("/")
    def index():
        html = (static_dir / "index.html").read_text(encoding="utf-8")
        return html.replace("__TOKEN__", TOKEN)

    @app.get("/shots/<path:name>")
    def shot(name):
        return send_from_directory(storage.data_dir() / "screenshots", name)

    @app.get("/api/state")
    def state():
        s = storage.load_settings()
        recipe = storage.load_recipe()
        try:
            auto = autostart.is_enabled()
        except Exception:
            auto = False
        return jsonify({
            "settings": s,
            "has_password": bool(storage.get_password(s["username"])),
            "recipe_verified": bool(recipe.get("verified")),
            "recipe_version": recipe.get("version"),
            "status": scheduler.status,
            "busy": scheduler.run_lock.locked(),
            "next_run": scheduler.next_run(s),
            "next_race": scheduler.next_race(s),
            "upcoming": scheduler.upcoming(s),
            "log": list(reversed(storage.load_log()))[:60],
            "autostart": auto,
            "days": storage.DAYS,
        })

    @app.post("/api/settings")
    def save():
        try:
            clean = validate(request.get_json(force=True) or {})
        except (ValueError, TypeError) as e:
            return jsonify({"ok": False, "message": str(e)}), 400
        old = storage.load_settings()
        before = storage.run_times(old)
        storage.save_settings(clean)
        if clean.get("paused") is False and old["paused"]:
            # Turning it on starts from the next run time, never a surprise run right now.
            scheduler.skip_passed_new_times([], storage.run_times(storage.load_settings()))
        elif "book_times" in clean:
            scheduler.skip_passed_new_times(before, clean["book_times"])
        return jsonify({"ok": True, "message": "Saved."})

    @app.post("/api/password")
    def password():
        body = request.get_json(force=True) or {}
        s = storage.load_settings()
        if not s["username"]:
            return jsonify({"ok": False, "message": "Save your username first."}), 400
        pw = str(body.get("password", ""))
        if not pw:
            return jsonify({"ok": False, "message": "Type your password first."}), 400
        try:
            storage.set_password(s["username"], pw)
        except Exception as e:
            return jsonify({"ok": False, "message": str(e)}), 500
        return jsonify({"ok": True, "message": "Password saved securely on this computer."})

    @app.post("/api/test-login")
    def test_login():
        s = storage.load_settings()
        pw = storage.get_password(s["username"])
        if not pw:
            return jsonify({"ok": False, "message": "Save your username and password first."}), 400
        if not scheduler.run_lock.acquire(blocking=False):
            return jsonify({"ok": False, "message": "A booking run is going. Try again in a minute."}), 409
        try:
            scheduler.status = "Testing sign-in"
            storage.refresh_recipe(s)
            result = booker.test_login(s, pw)
        except Exception as e:
            result = {"ok": False, "message": str(e)[:300]}
        finally:
            scheduler.status = "Idle"
            scheduler.run_lock.release()
        return jsonify(result)

    @app.post("/api/run")
    def run():
        body = request.get_json(force=True) or {}
        dry = bool(body.get("dry_run"))
        if scheduler.run_lock.locked():
            return jsonify({"ok": False, "message": "A booking run is already going."}), 409
        if body.get("race"):  # test midnight mode right now; always a dry run
            threading.Thread(target=scheduler.race_test, daemon=True).start()
            return jsonify({"ok": True, "message": "Midnight mode test started (dry run). Watch Activity below."})
        threading.Thread(target=scheduler.run, kwargs={"dry_run": dry}, daemon=True).start()
        return jsonify({"ok": True, "message": ("Dry run" if dry else "Booking") + " started. Watch Activity below."})

    @app.get("/api/team")
    def team():
        url = storage.load_settings().get("team_plan_url")
        if not url:
            return jsonify({"ok": False, "message": "Add the team plan link first."}), 400
        try:
            return jsonify({"ok": True, "plan": storage.fetch_plan(url)})
        except Exception as e:
            return jsonify({"ok": False, "message": f"Could not load the team plan: {e}"}), 502

    @app.post("/api/team/import")
    def team_import():
        s = storage.load_settings()
        try:
            plan = storage.fetch_plan(s["team_plan_url"])
        except Exception as e:
            return jsonify({"ok": False, "message": f"Could not load the team plan: {e}"}), 502
        me = next((p for p in plan.get("people", [])
                   if str(p.get("username", "")).lower() == s["username"].lower()), None)
        if not me:
            return jsonify({"ok": False, "message": f"'{s['username']}' is not in the team plan yet."}), 404
        body = {"slots": me.get("slots", [])}
        rooms = me.get("rooms") or plan.get("rooms", [])
        if rooms:  # blank rooms in the plan keep the rooms already set in the app
            body["rooms"] = rooms
        try:
            clean = validate(body)
        except ValueError as e:
            return jsonify({"ok": False, "message": f"The team plan has a problem: {e}"}), 400
        storage.save_settings(clean)
        return jsonify({"ok": True, "message": "Copied your rooms and time slots from the team plan."})

    @app.post("/api/autostart")
    def set_autostart():
        on = bool((request.get_json(force=True) or {}).get("enabled"))
        try:
            return jsonify({"ok": True, "enabled": autostart.set_enabled(on)})
        except Exception as e:
            return jsonify({"ok": False, "message": str(e)}), 500

    @app.post("/api/quit")
    def quit_app():
        threading.Timer(0.5, lambda: os._exit(0)).start()
        return jsonify({"ok": True, "message": "Canal Booker has stopped. Bookings will not run until you open it again."})

    return app
