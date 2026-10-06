"""Settings, activity log, recipe and password storage.

Everything personal lives in a per-user data folder, never in the repo:
  Windows: %APPDATA%/CanalBooker
  Mac:     ~/Library/Application Support/CanalBooker
Passwords go into the operating system keychain through `keyring`.
"""
import csv
import io
import json
import os
import re
import ssl
import sys
import threading
import datetime as dt
import urllib.request
from pathlib import Path

try:
    import keyring
except Exception:  # pragma: no cover
    keyring = None

try:
    import certifi
    _SSL = ssl.create_default_context(cafile=certifi.where())
except Exception:  # pragma: no cover
    _SSL = ssl.create_default_context()

APP_NAME = "CanalBooker"
KEYRING_SERVICE = "CanalBooker (Carleton booking portal)"
_lock = threading.RLock()

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

DEFAULT_SETTINGS = {
    "username": "",
    "rooms": [],            # ranked, first choice first
    "slots": [],            # {"day": "Mon", "start": "09:00", "end": "12:00", "enabled": true,
                            #  "alts": [{"start": "12:00", "end": "15:00"}]}  backup times, tried in order
    "book_time": "00:05",   # older single run time, used when book_times is empty
    "book_times": [],       # every time of day a booking run starts (Ottawa time)
    "days_ahead": 7,        # how far ahead the portal lets you book
    "retry_minutes": 20,    # keep retrying failed slots for this long
    "retry_every_seconds": 90,
    "event_title": "Group study",
    "attendees": 4,
    "team_plan_url": "",    # Google Sheet link or schedule.json link
    "recipe_url": "",       # where booking-step updates come from (portal_recipe.json on GitHub)
    "status_url": "",       # the team sheet's Apps Script web app; results show in its Status tab
    "race": True,           # midnight mode: sign in early, wait on the page, book the second the day opens
    "open_time": "00:00",   # when the portal opens a new day
    "race_lead_seconds": 150,    # how early to sign in and get the forms ready
    "race_window_seconds": 120,  # how long to keep checking after the open time
    "race_poll_ms": 1500,        # how often each ready form re-checks the calendar
    "show_browser": False,
    "paused": True,         # new installs start paused; the setup guide's last step turns it on
}


def resource_path(*parts) -> Path:
    """Files shipped with the app (works from source and from a packaged build)."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base.joinpath(*parts)


def data_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home()))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    d = Path(os.environ.get("CANALBOOKER_DATA", base / APP_NAME))
    (d / "screenshots").mkdir(parents=True, exist_ok=True)
    return d


def _read_json(path: Path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _write_json(path: Path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


# ---------- settings ----------

def load_settings() -> dict:
    with _lock:
        s = dict(DEFAULT_SETTINGS)
        s.update(_read_json(resource_path("app_defaults.json"), {}))
        s.update(_read_json(data_dir() / "settings.json", {}))
        return s


def save_settings(new: dict) -> dict:
    with _lock:
        s = load_settings()
        s.update({k: v for k, v in new.items() if k in DEFAULT_SETTINGS})
        _write_json(data_dir() / "settings.json", s)
        print(s)
        return s


def load_state() -> dict:
    with _lock:
        return _read_json(data_dir() / "state.json", {})


def save_state(state: dict):
    with _lock:
        _write_json(data_dir() / "state.json", state)


# ---------- activity log ----------

def load_log() -> list:
    with _lock:
        return _read_json(data_dir() / "log.json", [])


def add_log(entry: dict):
    with _lock:
        entry = {"ts": dt.datetime.now().isoformat(timespec="seconds"), **entry}
        log = load_log()
        log.append(entry)
        _write_json(data_dir() / "log.json", log[-500:])


def run_times(settings: dict) -> list:
    """The times of day a booking run starts, earliest first."""
    return sorted(settings.get("book_times") or [settings.get("book_time", "00:05")])


def slot_key(slot: dict) -> str:
    return f"{slot['day']} {slot['start']}-{slot['end']}"


def slot_times(slot: dict) -> list:
    """The main time first, then the backup times: [(start, end), ...]."""
    times = [(slot["start"], slot["end"])]
    for a in slot.get("alts") or []:
        if (a["start"], a["end"]) not in times:
            times.append((a["start"], a["end"]))
    return times


def booked_dates() -> set:
    """Days that already have a booking. The portal allows one 3 hour booking per person per day."""
    return {e.get("date") for e in load_log() if e.get("status") == "booked"}


# ---------- passwords ----------

def get_password(username: str):
    if not username or keyring is None:
        return None
    try:
        return keyring.get_password(KEYRING_SERVICE, username)
    except Exception:
        return None


def set_password(username: str, password: str):
    if keyring is None:
        raise RuntimeError("Secure password storage is not available on this computer.")
    keyring.set_password(KEYRING_SERVICE, username, password)


# ---------- shared files from GitHub or Google Sheets ----------

_SHEET = re.compile(r"docs\.google\.com/spreadsheets/d/([A-Za-z0-9_-]+)")


def is_google_sheet(url: str) -> bool:
    return bool(_SHEET.search(url or ""))


def to_raw_url(url: str) -> str:
    """Accept a normal GitHub file link or a Google Sheet link and turn it into a downloadable link."""
    url = (url or "").strip()
    if "github.com" in url and "/blob/" in url:
        url = url.replace("https://github.com/", "https://raw.githubusercontent.com/").replace("/blob/", "/", 1)
    m = _SHEET.search(url)
    if m:
        gid = re.search(r"[#&?]gid=(\d+)", url)
        url = f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=csv" + (f"&gid={gid.group(1)}" if gid else "")
    return url


def _fetch_text(url: str) -> str:
    req = urllib.request.Request(to_raw_url(url), headers={"User-Agent": "CanalBooker", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=10, context=_SSL) as r:
        return r.read().decode("utf-8-sig")


def fetch_json(url: str):
    return json.loads(_fetch_text(url))


def _norm_time(text: str) -> str:
    """'9:00', '09:00:00', '9:00 AM', '3 PM' -> 'HH:MM'. Returns '' if it is not a time."""
    m = re.match(r"^\s*(\d{1,2})(?::(\d{2}))?(?::\d{2})?\s*([AaPp])?\.?[Mm]?\.?\s*$", str(text or ""))
    if not m:
        return ""
    h, mins, ap = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
    if ap == "p" and h < 12:
        h += 12
    if ap == "a" and h == 12:
        h = 0
    return f"{h:02d}:{mins:02d}" if h < 24 and mins < 60 else ""


def parse_plan_csv(text: str) -> dict:
    """Turn the team plan sheet into the same shape as schedule.json.
    Rows above the header row (the one with Username and Day) are instructions and are skipped."""
    rows = list(csv.reader(io.StringIO(text)))
    head = None
    for i, row in enumerate(rows):
        lower = [c.strip().lower() for c in row]
        if "username" in lower and "day" in lower:
            head = i
            break
    if head is None:
        raise ValueError("The sheet needs a header row with Name, Username, Day, Start, End columns.")
    names = [c.strip().lower() for c in rows[head]]

    def col(row, *keys):
        for k in keys:
            for j, n in enumerate(names):
                if n.startswith(k) and j < len(row):
                    return row[j].strip()
        return ""

    def cols(row, key):
        """Every column starting with key, in order, as one comma list ("Backup 1", "Backup 2" or "Backup times")."""
        return ",".join(row[j].strip() for j, n in enumerate(names) if n.startswith(key) and j < len(row) and row[j].strip())

    people, order = {}, []
    for row in rows[head + 1:]:
        user = col(row, "username").split("@")[0]
        day = col(row, "day")[:3].title()
        start, end = _norm_time(col(row, "start")), _norm_time(col(row, "end"))
        if not user or day not in DAYS or not start or not end:
            continue
        key = user.lower()
        if key not in people:
            people[key] = {"name": col(row, "name") or user, "username": user, "slots": [], "rooms": []}
            order.append(key)
        alts = []
        for part in cols(row, "backup").split(","):
            if "-" in part:
                a, b = part.split("-", 1)
                if _norm_time(a) and _norm_time(b):
                    alts.append({"start": _norm_time(a), "end": _norm_time(b)})
        # Rooms belong to the row (that day); the person's first rooms are the fallback for rows without any.
        rooms = list(dict.fromkeys(r.strip() for r in cols(row, "room").split(",") if r.strip()))
        people[key]["slots"].append({"day": day, "start": start, "end": end, "alts": alts, "rooms": rooms})
        if rooms and not people[key]["rooms"]:
            people[key]["rooms"] = rooms
    plan_rooms = []
    for k in order:
        for r in people[k]["rooms"]:
            if r not in plan_rooms:
                plan_rooms.append(r)
    return {"rooms": plan_rooms, "people": [people[k] for k in order]}


def fetch_plan(url: str) -> dict:
    """Load the team plan from a Google Sheet or a schedule.json link."""
    if not is_google_sheet(url):
        return fetch_json(url)
    text = _fetch_text(url)
    if text.lstrip().lower().startswith(("<!doctype", "<html")):
        raise ValueError("Google did not share the sheet. In the sheet, click Share and set General access "
                         "to 'Anyone with the link' (Viewer).")
    try:
        return parse_plan_csv(text)
    except ValueError:
        # A link without a tab reads the first tab. If another tab (like the Setup guide) is in
        # front of the plan, look up the sheet's other tabs and use the first one with a plan.
        if re.search(r"[#&?]gid=\d+", url):
            raise
        for gid in _sheet_tab_ids(url):
            try:
                return parse_plan_csv(_fetch_text(url.split("#")[0] + f"#gid={gid}"))
            except Exception:
                continue
        raise


def _sheet_tab_ids(url: str) -> list:
    """The tab ids (gids) of a shared Google Sheet, read from its public view page."""
    sheet_id = _SHEET.search(url).group(1)
    try:  # not _fetch_text, which would turn this link into a CSV export
        req = urllib.request.Request(f"https://docs.google.com/spreadsheets/d/{sheet_id}/htmlview",
                                     headers={"User-Agent": "CanalBooker"})
        with urllib.request.urlopen(req, timeout=10, context=_SSL) as r:
            html = r.read().decode("utf-8", "replace")
    except Exception:
        return []
    ids = []
    for gid in re.findall(r"(?:gid(?:=|\\x3d|\\u003d|[\"']?\s*:\s*[\"']?)|sheet-button-)(\d+)", html):
        if gid not in ids:
            ids.append(gid)
    return ids[:20]


def shared_defaults(settings: dict) -> dict:
    """app_defaults.json from the main repo (next to recipe_url). Forks and older downloads
    use it to pick up shared links added later, such as status_url."""
    url = settings.get("recipe_url", "")
    if not url:
        return {}
    try:
        data = fetch_json(to_raw_url(url).rsplit("/", 1)[0] + "/app_defaults.json")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


RESULT_TEXT = {"booked": "Booked", "dry_run": "Test passed", "unavailable": "Not booked",
               "login_failed": "Sign-in failed", "error": "Not booked (problem)"}


def post_status(settings: dict, entry: dict, source: str, name: str = "", url: str = "") -> bool:
    """Add a row to the Status tab of the team sheet (through its Apps Script web app).
    Sends the username, date, room, time and result. Never the password."""
    url = url or settings.get("status_url") or shared_defaults(settings).get("status_url", "")
    if not url or not settings.get("username"):
        return False
    payload = {
        "username": settings["username"], "name": name,
        "date": entry.get("date", ""), "room": entry.get("room") or "", "time": entry.get("time") or "",
        "result": entry.get("result") or RESULT_TEXT.get(entry.get("status"), entry.get("status", "")),
        "details": entry.get("message", ""), "source": source,
    }
    try:
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": "CanalBooker"})
        with urllib.request.urlopen(req, timeout=20, context=_SSL) as r:
            return r.read(200).decode("utf-8", "replace").strip() == "ok"
    except Exception:
        return False


def load_recipe() -> dict:
    """Use the newest recipe: a downloaded team copy if it is newer than the built-in one."""
    bundled = _read_json(resource_path("portal_recipe.json"), {})
    downloaded = _read_json(data_dir() / "portal_recipe.json", {})
    if downloaded.get("version", 0) > bundled.get("version", 0):
        return downloaded
    return bundled


def refresh_recipe(settings: dict):
    """Grab the newest portal_recipe.json: from recipe_url, or else from the same GitHub folder as the team plan."""
    url = settings.get("recipe_url", "")
    if not url:
        plan = settings.get("team_plan_url", "")
        if not plan or is_google_sheet(plan):
            return
        url = to_raw_url(plan).rsplit("/", 1)[0] + "/portal_recipe.json"
    try:
        recipe = fetch_json(url)
        if isinstance(recipe, dict) and "book" in recipe:
            with _lock:
                _write_json(data_dir() / "portal_recipe.json", recipe)
    except Exception:
        pass
