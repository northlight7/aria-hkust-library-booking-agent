"""
booking_agent.py — a command-line agent that checks and books HKUST library rooms.

WHAT IT DOES
  available : show which rooms are free (and when) on a given day
  book      : reserve a specific room + time slot (needs you to log in first)
  login     : open a browser once so you can log in with HKUST SSO;
              we save the session so 'book' can use it

EXAMPLES
  python booking_agent.py available --date 2026-07-15 --area 3
  python booking_agent.py available --date 2026-07-15 --area 3 --room 126
  python booking_agent.py book --date 2026-07-15 --area 3 --room 126 \
        --time 14:00-15:00 --title "Group meeting"
  python booking_agent.py login

WHY TWO STEPS FOR BOOKING?
  Reading the calendar is public, so 'available' just works.
  Booking is behind HKUST login (CAS/Shibboleth). We do NOT store your password.
  Instead, you log in once in a real browser; we keep the session cookie in a
  local file (auth_state.json) and reuse it. Delete that file to "log out".
"""

from __future__ import annotations
import argparse
import json
import os
import sys

from mrbs import (
    Mrbs, AREAS, DayView, SessionExpired, BookingError,
    time_to_seconds, SLOT_MINUTES,
)

# Where we remember your login between runs.
AUTH_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "auth_state.json")

# Small helpers for coloured-ish plain output (works in any terminal).
FREE = "."      # free slot
BUSY = "X"      # someone booked it
BLOCK = "#"     # library closed / unbookable


# ---------------------------------------------------------------------------
# Loading a saved login
# ---------------------------------------------------------------------------
def load_cookies() -> dict | None:
    """Read cookies we saved from the 'login' step (Playwright storage_state)."""
    if not os.path.exists(AUTH_FILE):
        return None
    try:
        with open(AUTH_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None

    # Playwright saves cookies as a list; we only want the ones for our site.
    cookies = {}
    for c in state.get("cookies", []):
        if "lbbooking.hkust.edu.hk" in c.get("domain", ""):
            cookies[c["name"]] = c["value"]
    # Also support a simple hand-made {"MRBS_SESSID": "..."} file.
    if not cookies and isinstance(state, dict):
        for k, v in state.items():
            if isinstance(v, str):
                cookies[k] = v
    return cookies or None


# ---------------------------------------------------------------------------
# Command: available
# ---------------------------------------------------------------------------
def cmd_available(args) -> int:
    client = Mrbs()
    print(f"Checking {AREAS.get(args.area, 'area ' + args.area)} "
          f"on {args.date} ...\n")
    try:
        day = client.day_view(args.date, args.area)
    except Exception as e:
        print(f"Could not load the calendar: {e}")
        return 1

    if not day.rooms:
        print("No rooms found for that area/date.")
        return 1

    if args.room:
        return _show_one_room(day, args.room)
    return _show_all_rooms(day)


def _show_all_rooms(day: DayView) -> int:
    """One line per room: a strip of . X # showing the whole day at a glance."""
    # Build the shared list of time labels from the first room.
    any_room = day.rooms[0].id
    times = [s.time for s in day.slots[any_room]]
    if times:
        print(f"Times run {times[0]} to {times[-1]} "
              f"in {SLOT_MINUTES}-minute steps.")
        print(f"Legend:  {FREE}=free   {BUSY}=booked   {BLOCK}=closed\n")

    for room in day.rooms:
        strip = ""
        free_count = 0
        for slot in day.slots[room.id]:
            if slot.status == "free":
                strip += FREE
                free_count += 1
            elif slot.status == "busy":
                strip += BUSY
            else:
                strip += BLOCK
        name = f"{room.name} (seats {room.capacity})" if room.capacity else room.name
        print(f"  {name:<28} {strip}   {free_count} free")

    print("\nTip: add --room <name or id> to see one room's exact free times.")
    return 0


def _show_one_room(day: DayView, room_key: str) -> int:
    room = day.room_by_key(room_key)
    if room is None:
        print(f"Room '{room_key}' not found in this area. Available rooms:")
        for r in day.rooms:
            print(f"   {r}")
        return 1

    cap = f" (seats {room.capacity})" if room.capacity else ""
    print(f"Full day for room {room.name}{cap}  [id={room.id}]\n")

    # Group consecutive free slots into readable ranges.
    free_ranges = _free_ranges(day.slots[room.id])
    for slot in day.slots[room.id]:
        mark = {"free": FREE, "busy": BUSY, "blocked": BLOCK}[slot.status]
        extra = f"  <- {slot.label}" if slot.label else ""
        print(f"   {slot.time}  {mark} {slot.status}{extra}")

    print()
    if free_ranges:
        print("Free blocks you could book:")
        for start, end in free_ranges:
            print(f"   {start} - {end}")
    else:
        print("No free time on this day.")
    return 0


def _free_ranges(slots):
    """Turn a run of free 30-min slots into (start, end) time ranges."""
    ranges = []
    run_start = None
    prev_time = None
    for slot in slots:
        if slot.status == "free":
            if run_start is None:
                run_start = slot.time
            prev_time = slot.time
        else:
            if run_start is not None:
                ranges.append((run_start, _add_minutes(prev_time, SLOT_MINUTES)))
                run_start = None
    if run_start is not None:
        ranges.append((run_start, _add_minutes(prev_time, SLOT_MINUTES)))
    return ranges


def _add_minutes(hhmm: str, minutes: int) -> str:
    h, m = map(int, hhmm.split(":"))
    total = h * 60 + m + minutes
    return f"{(total // 60) % 24:02d}:{total % 60:02d}"


# ---------------------------------------------------------------------------
# Command: book
# ---------------------------------------------------------------------------
def cmd_book(args) -> int:
    # 1. Understand the requested time, e.g. "14:00-15:00".
    try:
        start_str, end_str = args.time.split("-")
        start_str, end_str = start_str.strip(), end_str.strip()
        start_sec = time_to_seconds(start_str)
        end_sec = time_to_seconds(end_str)
    except ValueError:
        print("--time must look like 14:00-15:00")
        return 1
    if end_sec <= start_sec:
        print("The end time must be after the start time.")
        return 1

    # 2. MOCK MODE: run the whole booking flow WITHOUT logging in or touching
    #    the live site's booking system. Used for teaching/demos and testing.
    if args.mock:
        return _book_mock(args, start_str, end_str, start_sec, end_sec)

    # Do we have a saved login?
    cookies = load_cookies()
    if not cookies:
        print("You are not logged in yet. Run this first:\n")
        print("    python booking_agent.py login\n")
        return 1
    client = Mrbs(cookies=cookies)

    if not client.is_logged_in():
        print("Your saved login has expired. Please run 'login' again:\n")
        print("    python booking_agent.py login\n")
        return 1

    # 3. Check the slot is actually free before trying (avoids ugly errors).
    day = client.day_view(args.date, args.area)
    room = day.room_by_key(args.room)
    if room is None:
        print(f"Room '{args.room}' not found in area {args.area}.")
        return 1
    conflict = _first_conflict(day.slots[room.id], start_str, end_str)
    if conflict:
        print(f"Sorry, {room.name} is not free at {conflict} on {args.date}.")
        print("Run 'available' to see open times.")
        return 1

    # 4. Load the real booking form (this is where we need the login).
    hour, minute = map(int, start_str.split(":"))
    try:
        form = client.get_booking_form(room.id, args.date, hour, minute)
    except SessionExpired:
        print("Your session expired. Please run 'login' again.")
        return 1

    # 5. Fill in only the fields we care about; keep everything else the site
    #    gave us (including the csrf_token, which we must not change).
    form["name"] = args.title
    form["description"] = args.title
    form["start_seconds"] = str(start_sec)
    form["end_seconds"] = str(end_sec - SLOT_MINUTES * 60) if _uses_periods(form) else str(end_sec)
    form["start_date"] = args.date
    form["end_date"] = args.date
    form.setdefault("rep_type", "0")

    if args.dry_run:
        print("DRY RUN — this is exactly what WOULD be sent (no booking made):\n")
        _print_payload(form)
        return 0

    # 6. Send it.
    try:
        result = client.submit_booking(form)
    except SessionExpired:
        print("Your session expired during booking. Run 'login' again.")
        return 1
    except BookingError as e:
        print(f"Booking failed: {e}")
        return 1

    print("Booking confirmed!")
    print(f"   Room:  {room.name} (id {room.id})")
    print(f"   Date:  {args.date}")
    print(f"   Time:  {start_str} - {end_str}")
    print(f"   Title: {args.title}")
    print(f"   (Server said: {result})")
    return 0


def _book_mock(args, start_str, end_str, start_sec, end_sec) -> int:
    """Demonstrate the FULL booking flow safely — no login, no live write.

    We still read real availability (that part is public), but the booking form
    comes from a local sample file and the "submit" is simulated. This lets
    students see exactly what a booking does without reserving a real room.
    """
    import os
    from mrbs import _read_form_fields  # reuse the real form parser

    print("=== MOCK MODE: no login, no real booking will be made ===\n")

    # Read REAL availability so the free/busy check is genuine.
    client = Mrbs()
    try:
        day = client.day_view(args.date, args.area)
    except Exception as e:
        print(f"Could not load the calendar: {e}")
        return 1
    room = day.room_by_key(args.room)
    if room is None:
        print(f"Room '{args.room}' not found in area {args.area}.")
        return 1
    conflict = _first_conflict(day.slots[room.id], start_str, end_str)
    if conflict:
        print(f"(real data) {room.name} is NOT free at {conflict} on {args.date}.")
        print("In mock mode we stop here, just like the real command would.")
        return 1
    print(f"(real data) {room.name} is free for {start_str}-{end_str}. Good.\n")

    # Load the booking form from a local sample file (stands in for the real
    # authenticated edit_entry.php page).
    fixture = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "assets", "sample_edit_form.html")
    with open(fixture, "r", encoding="utf-8") as f:
        form = _read_form_fields(f.read())

    # Fill in the same fields the real command would.
    form["name"] = args.title
    form["description"] = args.title
    form["start_seconds"] = str(start_sec)
    form["end_seconds"] = str(end_sec)
    form["start_date"] = args.date
    form["end_date"] = args.date
    form.setdefault("rep_type", "0")

    print("This is the form data that WOULD be POSTed to edit_entry_handler.php:")
    _print_payload(form)

    # Simulate the server's success response (a redirect back to the calendar).
    y, m, d = args.date.split("-")
    fake_redirect = f"day.php?year={int(y)}&month={int(m)}&day={int(d)}&area={args.area}"
    print("\n(simulated) Server accepted the booking and redirected to:")
    print(f"   {fake_redirect}")
    print("\nMOCK booking 'confirmed' (nothing was actually reserved).")
    return 0


def _first_conflict(slots, start_str, end_str):
    """Return the first requested time that is NOT free, or None if all free."""
    want = _times_in_range(start_str, end_str)
    by_time = {s.time: s for s in slots}
    for t in want:
        slot = by_time.get(t)
        if slot is None or slot.status != "free":
            return t
    return None


def _times_in_range(start_str, end_str):
    """List the 30-min slot start times covered by start..end."""
    out = []
    cur = start_str
    while time_to_seconds(cur) < time_to_seconds(end_str):
        out.append(cur)
        cur = _add_minutes(cur, SLOT_MINUTES)
    return out


def _uses_periods(form: dict) -> bool:
    """Some MRBS sites book by named 'periods' instead of by minutes.

    We detect it loosely: if the form has a period selector, treat end_seconds
    as 'start of last period'. HKUST appears to use minutes, but this keeps us
    safe either way.
    """
    return any("period" in k.lower() for k in form)


def _print_payload(form: dict):
    for k, v in form.items():
        shown = v if len(str(v)) < 60 else str(v)[:57] + "..."
        print(f"   {k} = {shown}")


# ---------------------------------------------------------------------------
# Command: login
# ---------------------------------------------------------------------------
def cmd_login(args) -> int:
    """Open a real browser so the user can log in with HKUST SSO, then save it."""
    area = getattr(args, "area", "8")
    return _do_login(interactive=True, area=area)


def login_noninteractive(area: str = "8") -> bool:
    """Same as cmd_login but auto-detects completion; no terminal input needed.

    Returns True if cookies were saved successfully.
    """
    return _do_login(interactive=False, area=area)


def _do_login(interactive: bool, area: str = "8") -> int | bool:
    """Shared login implementation.

    When interactive, prints progress and waits for the user to press Enter
    before saving cookies. When non-interactive (called from the web server),
    polls the browser URL until the SSO redirect chain has returned to the
    MRBS site, then saves cookies and closes automatically.

    After the SSO dance completes, we verify the browser landed on a day.php
    calendar page. If the SSO redirect chain dropped query parameters (which
    happens when MRBS doesn't URL-encode the service URL properly), the
    browser may have landed on a different MRBS page. We navigate to the
    correct day.php before saving cookies so the user sees the calendar.
    """
    import time as _time

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright is not installed. Run:  pip install playwright")
        print("Then:  playwright install chromium")
        return 1 if interactive else False

    if interactive:
        print("Opening a browser window ...")
        print("Log in with your HKUST account (Microsoft sign-in). When you can see")
        print("the room calendar, come back here and press Enter.\n")

    from datetime import date as _date
    today = _date.today()
    # Put year/month/day BEFORE area in the query string. The SSO chain
    # passes this URL as a CAS service= parameter. If MRBS doesn't properly
    # URL-encode it, everything after the first & looks like CAS params.
    # Leading with the date fields makes it more likely the area value
    # survives even if the rest is truncated.
    target_url = (f"{__import__('mrbs').BASE_URL}/day.php"
                  f"?year={today.year}&month={today.month}"
                  f"&day={today.day}&area={area}")

    with sync_playwright() as p:
        # Prefer Chromium on macOS — it opens as a recognizable app window
        # that the user can Cmd+Tab to, unlike WebKit which is easy to lose
        # behind a fullscreen browser. Chromium elsewhere as before.
        import platform as _platform
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto(target_url)
        # Try to bring the sign-in window to the foreground. This is a
        # best-effort hint — macOS Spaces isolation when Safari is
        # fullscreen means the window will always open in a separate Space.
        # The UI text tells the user to look for it.
        _time.sleep(1)
        try:
            page.bring_to_front()
        except Exception:
            pass

        if interactive:
            input("Press Enter here AFTER you have logged in and see the calendar... ")
        else:
            deadline = _time.time() + 180  # 3 minutes
            closed = False
            while _time.time() < deadline:
                try:
                    on_mrbs = page.evaluate(
                        "window.location.hostname.includes('lbbooking.hkust.edu.hk')"
                    )
                except Exception as e:
                    # "Target closed" means the user closed the browser window.
                    if "closed" in str(e).lower() or "target" in str(e).lower():
                        closed = True
                        break
                    on_mrbs = False
                if on_mrbs:
                    _time.sleep(2)
                    break
                _time.sleep(2)
            else:
                # 3-minute timeout — check if we at least landed on MRBS.
                if "lbbooking.hkust.edu.hk" not in page.url:
                    closed = True

            if closed:
                try:
                    browser.close()
                except Exception:
                    pass
                return 1 if interactive else False

            # The SSO redirect chain may have landed us on the wrong MRBS
            # page (e.g. a specific room or the homepage) if query
            # parameters were dropped. Navigate to the correct calendar
            # page so the user sees what they expect.
            if "day.php" not in page.url:
                page.goto(target_url)
                _time.sleep(1)

        context.storage_state(path=AUTH_FILE)
        browser.close()

    cookies = load_cookies()
    if not cookies:
        if interactive:
            print("\nHmm, no session cookie was saved. Did the login finish?")
        return 1 if interactive else False

    # Verify the session actually works (not just that cookies exist).
    from mrbs import Mrbs
    client = Mrbs(cookies=cookies)
    if not client.is_logged_in():
        if interactive:
            print("\nYour login was saved but the session is not active.")
            print("The site may have rejected it. Try logging in again.")
        return 1 if interactive else False

    if interactive:
        print(f"\nSaved your login to {AUTH_FILE}")
        print("You can now use Aria to check and book rooms.")
    return 0 if interactive else True


# ---------------------------------------------------------------------------
# Wiring up the command-line arguments
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Check and book HKUST library rooms from the command line.")
    sub = p.add_subparsers(dest="command", required=True)

    a = sub.add_parser("available", help="show free/busy rooms for a day")
    a.add_argument("--date", required=True, help="YYYY-MM-DD")
    a.add_argument("--area", required=True, help="area number, e.g. 3")
    a.add_argument("--room", help="optional: one room's id or name")
    a.set_defaults(func=cmd_available)

    b = sub.add_parser("book", help="book a room + time slot")
    b.add_argument("--date", required=True, help="YYYY-MM-DD")
    b.add_argument("--area", required=True, help="area number, e.g. 3")
    b.add_argument("--room", required=True, help="room id or name")
    b.add_argument("--time", required=True, help="e.g. 14:00-15:00")
    b.add_argument("--title", required=True, help="a name for your booking")
    b.add_argument("--dry-run", action="store_true",
                   help="show what would be sent, but do NOT book")
    b.add_argument("--mock", action="store_true",
                   help="demo the full booking flow with no login and no real "
                        "booking (uses real availability + a sample form)")
    b.set_defaults(func=cmd_book)

    lg = sub.add_parser("login", help="log in once with HKUST SSO")
    lg.add_argument("--area", default="8", help="area to show after login, e.g. 8 (default: 8)")
    lg.set_defaults(func=cmd_login)

    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
