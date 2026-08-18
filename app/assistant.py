"""
assistant.py : Aria, the HKUST library room booking assistant.

Aria is agentic: the LLM is given real TOOLS it can call (get room summaries,
check availability, search rooms, book and cancel rooms), so it can answer
arbitrary questions like:

  "How many rooms are available in total?"
  "Which room has the largest capacity?"
  "Any empty Learning Commons rooms tomorrow at 3pm?"
  "What's free this week for 2 hours?"
  "Book 1-350 from 2 to 4 pm for my study group."
  "Cancel my Group meeting booking in 1-350 tomorrow."

Voice (STT and TTS) lives in the browser via the Web Speech API. This module
is voice-free by design: one voice pipeline, not two.
"""

from __future__ import annotations

import json
import threading
import time as _time
from datetime import date, timedelta

import config as cfg_mod

# ════════════════════════════════════════════════════════════════════
# Persona
# ════════════════════════════════════════════════════════════════════
SYSTEM_PROMPT = """You are Aria, a voice assistant for HKUST library room booking. Your replies are spoken aloud, so they MUST be brief.

ALWAYS follow these rules:
- Be ruthlessly brief. Yes/no questions get a one-word answer: "Yes" or "No."
- Never recite the tool's free-block times to the user. "Is LC-03 free from 6 to 8" → "Yes." Not "Yes, it's free from 5 to 8:30." The user asked about 6-8, answer about 6-8.
- After answering, you may offer to book in one short sentence. "Yes. Want me to book it?"
- No greetings, sign-offs, markdown, bullet points, emojis, em dashes, or semicolons.
- If you don't have the data, call a tool. Never invent room numbers or availability.
- Ambiguous questions: ask a short clarifying question instead of guessing.
- Duration queries ("2-hour slot"): always pass min_hours to the tool.
- Booking: just do it. No "shall I?" or "confirm?" : book immediately and report the result. Never ask for a title; always use "Study." Say "6 PM" not "18:00."
- Cancelling: if the user names a specific booking or says "cancel my booking," just cancel it immediately : no confirmation. Only ask "which one?" if there are several and the user didn't specify. Never say "Confirm?" and then answer yourself.
- Unrelated requests: one short sentence declining, nothing more.
- Dates: today is {today}. Convert "tomorrow", "next Monday" etc. to YYYY-MM-DD for tool calls. But in your SPOKEN reply, never say YYYY-MM-DD : say "today", "tomorrow", or "Friday."
- Times: convert 24-hour tool times like "18:00" to spoken form like "6 PM" or "6 in the evening." Never say "eighteen hundred" or "eighteen zero zero."
- Skip the date entirely when it is today. "Booked from 6 to 8 PM" is enough.

HKUST context (do NOT recite this to the user unless asked):
- Booking rules: maximum 2 hours per booking, in 30-minute increments. Each student gets at most 2 bookings per week. Enforce these : reject requests that break the rules.
- 8 student-bookable areas. When calling tools, use the numeric area ID:
  area 3 = Group Study Rooms (37 rooms, capacities 4-16)
  area 8 = LC Study Rooms (18 rooms, capacities 6-10)
  area 20 = Study Pods (9 pods)
  area 19 = Nap Pods (2 pods)
  area 10 = Creative Media Zone (5 rooms)
  area 13 = Computers (5)
  area 14 = 3D Printers (2)
  area 21 = Curved Monitors (34)
  area 6 = Teaching Venues (faculty only, never mention unless asked)
- Students book with their ITSC account. Confirmation email is sent automatically.
- Teaching Venues are faculty-only. Never mention them unless asked.
"""

# ════════════════════════════════════════════════════════════════════
# Tool definitions (OpenAI function-calling schema)
# ════════════════════════════════════════════════════════════════════
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_areas",
            "description": "List all room types (areas) at the HKUST library: Group Study Rooms, LC Study Rooms, Study Pods, Nap Pods, Creative Media Zone, Teaching Venues, Computers, 3D Printers, Curved Monitors. Use when asked about room types, what kinds of rooms exist, or what areas are available.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_rooms_summary",
            "description": "Get totals: how many rooms exist in an area, how many have free time, capacity range. Use for questions like 'how many rooms', 'which is biggest', 'total availability'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "area": {"type": "string", "description": "Area number, e.g. '3'", "default": "3"},
                    "date": {"type": "string", "description": "YYYY-MM-DD", "default": "today"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_availability",
            "description": "Check which rooms are free on a date, optionally for one specific room. Returns free blocks. Use min_hours to filter by minimum duration (e.g. min_hours=2 for 2-hour slots).",
            "parameters": {
                "type": "object",
                "properties": {
                    "area": {"type": "string", "default": "3"},
                    "date": {"type": "string"},
                    "room": {"type": "string", "description": "Optional room name or id"},
                    "min_hours": {"type": "number", "description": "Optional: only return blocks at least this many hours long"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_week",
            "description": "Check availability across several consecutive days (default 3). Use for 'this week', 'next few days', 'when is X free'. Returns a per-day summary, or per-day free blocks when a room is given.",
            "parameters": {
                "type": "object",
                "properties": {
                    "area": {"type": "string", "default": "3"},
                    "start_date": {"type": "string", "description": "YYYY-MM-DD, first day to check"},
                    "days": {"type": "integer", "description": "How many days to check, 1-7", "default": 7},
                    "room": {"type": "string", "description": "Optional room name or id"},
                    "min_hours": {"type": "number", "description": "Optional: only count blocks at least this many hours long"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_rooms",
            "description": "Search rooms in an area by capacity range or name. Use for 'a room for 8 people', 'rooms with LG3 in the name', 'the biggest free room'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "area": {"type": "string", "default": "3"},
                    "date": {"type": "string"},
                    "min_capacity": {"type": "integer", "description": "Minimum seats"},
                    "max_capacity": {"type": "integer", "description": "Maximum seats"},
                    "name_contains": {"type": "string", "description": "Substring of the room name, e.g. 'LG3'"},
                    "min_hours": {"type": "number", "description": "Optional: only count free blocks at least this long"},
                    "only_free": {"type": "boolean", "description": "If true, only rooms with at least one free block", "default": True},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_room_details",
            "description": "Get details for one room: capacity, exact free time blocks on a date. Use min_hours to filter.",
            "parameters": {
                "type": "object",
                "properties": {
                    "room": {"type": "string", "description": "Room name (e.g. '1-350') or id"},
                    "date": {"type": "string"},
                    "area": {"type": "string", "default": "3"},
                    "min_hours": {"type": "number", "description": "Optional: only return blocks at least this many hours long"},
                },
                "required": ["room"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "book_room",
            "description": "Book a room. Requires the user to be signed in. Title defaults to 'Study'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "room": {"type": "string"},
                    "date": {"type": "string"},
                    "time": {"type": "string", "description": "e.g. '14:00-16:00'"},
                    "title": {"type": "string"},
                    "area": {"type": "string", "default": "3"},
                },
                "required": ["room", "date", "time", "title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_bookings",
            "description": "Find bookings. Omit date to scan the next 3 days. Omit area to check all areas. Set mine=true to only return the current user's own bookings. Returns entry ids needed for cancellation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "Optional: YYYY-MM-DD. Omit to scan next 7 days."},
                    "area": {"type": "string", "description": "Optional: area id. Omit to check all areas."},
                    "room": {"type": "string", "description": "Optional room name or id"},
                    "search": {"type": "string", "description": "Optional text to match in the booking title"},
                    "mine": {"type": "boolean", "description": "Set to true to only return the current user's own bookings."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_booking",
            "description": "Cancel one booking by its entry id (from find_bookings). Requires the user to be signed in. Only the user's own bookings can be cancelled, the site enforces ownership. Confirm with the user before calling this.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entry_id": {"type": "string", "description": "The MRBS entry id"},
                },
                "required": ["entry_id"],
            },
        },
    },
]

# Speech-to-text often mishears library abbreviations. Map common errors
# back to what the user actually said before the text reaches the LLM.
_SPEECH_FIXES = {
    "elsie": "LC",
    "el C": "LC",
    "ell see": "LC",
    "ell c": "LC",
    "al see": "LC",
    "l c": "LC",           # "LC" read letter-by-letter but joined
    "el cee": "LC",
}

# Areas the assistant knows about (name -> id) for natural language mapping.
# Ordered longest-key-first so "LC Study Rooms" matches "lc study" and not
# the bare "study" key (which would resolve to Group Study, area 3).
AREA_NAMES = [
    ("learning commons", "8"), ("lc study", "8"),
    ("group study", "3"),
    ("study pod", "20"), ("study pods", "20"),
    ("creative media", "10"),
    ("curved monitor", "21"), ("curved monitors", "21"),
    ("nap pod", "19"), ("nap pods", "19"),
    ("3d printer", "14"), ("3d printers", "14"),
    ("lc", "8"), ("study", "3"), ("group", "3"),
    ("pod", "20"), ("pods", "20"),
    ("nap", "19"),
    ("media", "10"), ("creative", "10"),
    ("computer", "13"), ("computers", "13"),
    ("teaching", "6"), ("venue", "6"),
]

# How long a cached day view stays usable when the site is unreachable.
CACHE_STALE_SECONDS = 15 * 60


# ════════════════════════════════════════════════════════════════════
# Day-view cache: last-known data survives a site outage
# ════════════════════════════════════════════════════════════════════
_day_cache: dict[tuple[str, str], tuple[float, object]] = {}
_day_cache_lock = threading.Lock()


def _get_day(d: str, area: str):
    """Fetch a day view with one retry, falling back to cached data.

    Returns (day_view, stale: bool). Raises the original error only when
    nothing cached is available either.

    SessionExpired (auth wall, not a transient network issue) is never
    retried and bypasses the cache fallback — no cached data helps when
    the user needs to sign in.
    """
    from mrbs import Mrbs, SessionExpired
    from booking_agent import load_cookies

    cookies = load_cookies()

    key = (d, area)
    last_err: Exception | None = None
    for attempt in range(2):
        try:
            day = Mrbs(cookies=cookies).day_view(d, area)
            with _day_cache_lock:
                _day_cache[key] = (_time.time(), day)
            return day, False
        except SessionExpired:
            raise  # auth failures are not transient — surface immediately
        except Exception as e: # network or parse trouble: retry once
            last_err = e
            if attempt == 0:
                _time.sleep(0.8)

    with _day_cache_lock:
        cached = _day_cache.get(key)
    if cached and (_time.time() - cached[0]) < CACHE_STALE_SECONDS:
        return cached[1], True
    raise last_err  # type: ignore[misc]


# ════════════════════════════════════════════════════════════════════
# Tool implementations : call the real backend (mrbs)
# ════════════════════════════════════════════════════════════════════
def _bust_cache(date_str: str | None = None, area_str: str | None = None) -> None:
    """Clear the day-view cache so the next check sees live data.

    Called after booking or cancelling so stale cached data doesn't make
    the model contradict itself on the next query.
    """
    with _day_cache_lock:
        if date_str and area_str:
            _day_cache.pop((date_str, area_str), None)
        elif date_str:
            for (d, a) in list(_day_cache.keys()):
                if d == date_str:
                    del _day_cache[(d, a)]
        else:
            _day_cache.clear()


def _fix_speech(text: str) -> str:
    """Correct common speech-to-text misrecognitions in the user's input."""
    low = text.lower()
    for wrong, right in _SPEECH_FIXES.items():
        wlow = wrong.lower()
        idx = low.find(wlow)
        while idx != -1:
            before_ok = idx == 0 or not low[idx - 1].isalpha()
            after = idx + len(wrong)
            after_ok = after == len(text) or not low[after].isalpha()
            if before_ok and after_ok:
                text = text[:idx] + right + text[after:]
                low = text.lower()
            idx = low.find(wlow, idx + len(right))
    return text


def _resolve_area(text: str | None) -> str:
    """Map free text like 'learning commons' or '3' to an area id."""
    if not text:
        return "3"
    t = str(text).strip().lower()
    if t.isdigit():
        return t
    for key, area_id in AREA_NAMES:
        if key in t:
            return area_id
    return "3"


def _norm_date(d: str | None) -> str:
    if not d or d == "today":
        return date.today().strftime("%Y-%m-%d")
    return d


def _to_minutes(hhmm: str) -> int:
    """'14:00' -> 840 (minutes since midnight)."""
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _filter_ranges(ranges: list, min_h: float) -> list:
    """Keep only (start, end) ranges spanning at least min_h hours."""
    if not min_h:
        return ranges
    return [(s, e) for s, e in ranges
            if (_to_minutes(e) - _to_minutes(s)) >= min_h * 60]


def tool_list_areas() -> str:
    """Return all room types with booking eligibility."""
    from mrbs import AREAS
    FACULTY_ONLY = {"6"}
    return json.dumps([
        {"id": k, "name": v, "bookable_by_students": k not in FACULTY_ONLY}
        for k, v in AREAS.items()
    ])


def tool_get_rooms_summary(area: str = "3", date: str = None) -> str:
    from mrbs import AREAS

    area = _resolve_area(area)
    d = _norm_date(date)
    try:
        day, stale = _get_day(d, area)
    except Exception as e:
        return json.dumps({"error": f"Could not load data: {e}"})

    total = len(day.rooms)
    free_rooms = 0
    caps = []
    for r in day.rooms:
        free = sum(1 for s in day.slots[r.id] if s.status == "free")
        if free >= 2:
            free_rooms += 1
        if r.capacity and r.capacity.isdigit():
            caps.append((int(r.capacity), r.name))

    caps.sort(reverse=True)
    biggest = caps[0] if caps else None
    smallest = caps[-1] if caps else None
    result = {
        "area": AREAS.get(area, area),
        "date": d,
        "total_rooms": total,
        "rooms_with_free_time": free_rooms,
        "largest_room": {"name": biggest[1], "seats": biggest[0]} if biggest else None,
        "smallest_room": {"name": smallest[1], "seats": smallest[0]} if smallest else None,
    }
    if stale:
        result["warning"] = "Live site unreachable, this is cached data from the last few minutes."
    return json.dumps(result)


def tool_check_availability(area: str = "3", date: str = None, room: str = None, min_hours: float = 0) -> str:
    from mrbs import AREAS
    import booking_agent as ba

    area = _resolve_area(area)
    d = _norm_date(date)
    try:
        day, stale = _get_day(d, area)
    except Exception as e:
        return json.dumps({"error": f"Could not load data: {e}"})

    if room:
        r = day.room_by_key(room)
        if not r:
            return json.dumps({"error": f"Room '{room}' not found"})
        ranges = _filter_ranges(ba._free_ranges(day.slots[r.id]), min_hours)
        out = {
            "room": r.name, "seats": r.capacity, "date": d,
            "free_blocks": [f"{s}-{e}" for s, e in ranges],
            "min_hours_filter": min_hours or None,
        }
        if stale:
            out["warning"] = "Live site unreachable, this is cached data."
        return json.dumps(out)

    # Summarize: rooms with meaningful free time
    rooms = []
    for r in day.rooms:
        ranges = _filter_ranges(ba._free_ranges(day.slots[r.id]), min_hours)
        if ranges:
            rooms.append({"room": r.name, "seats": r.capacity,
                          "free_blocks": [f"{s}-{e}" for s, e in ranges[:3]]})
    out = {
        "area": AREAS.get(area, area), "date": d,
        "free_room_count": len(rooms),
        "min_hours_filter": min_hours or None,
        "rooms": rooms[:8],
    }
    if stale:
        out["warning"] = "Live site unreachable, this is cached data."
    return json.dumps(out)


def tool_check_week(area: str = "3", start_date: str = None, days: int = 3,
                    room: str = None, min_hours: float = 0) -> str:
    from mrbs import AREAS
    import booking_agent as ba

    area = _resolve_area(area)
    start = _norm_date(start_date)
    try:
        days = max(1, min(int(days or 7), 7))
    except (TypeError, ValueError):
        days = 7

    try:
        first = date.fromisoformat(start)
    except ValueError:
        return json.dumps({"error": f"Date '{start}' is not YYYY-MM-DD."})

    out_days = []
    errors = 0
    for i in range(days):
        d = (first + timedelta(days=i)).strftime("%Y-%m-%d")
        try:
            day, _stale = _get_day(d, area)
        except Exception:
            errors += 1
            out_days.append({"date": d, "error": "unreachable"})
            continue

        if room:
            r = day.room_by_key(room)
            if not r:
                return json.dumps({"error": f"Room '{room}' not found"})
            ranges = _filter_ranges(ba._free_ranges(day.slots[r.id]), min_hours)
            out_days.append({"date": d,
                             "free_blocks": [f"{s}-{e}" for s, e in ranges[:4]]})
        else:
            free_rooms = 0
            for rm in day.rooms:
                if _filter_ranges(ba._free_ranges(day.slots[rm.id]), min_hours):
                    free_rooms += 1
            out_days.append({"date": d, "free_room_count": free_rooms})

    result = {
        "area": AREAS.get(area, area),
        "room": room or None,
        "min_hours_filter": min_hours or None,
        "days": out_days,
    }
    if errors:
        result["warning"] = f"{errors} day(s) could not be loaded."
    return json.dumps(result)


def tool_search_rooms(area: str = "3", date: str = None, min_capacity: int = 0,
                      max_capacity: int = 0, name_contains: str = None,
                      min_hours: float = 0, only_free: bool = True) -> str:
    from mrbs import AREAS
    import booking_agent as ba

    area = _resolve_area(area)
    d = _norm_date(date)
    try:
        day, stale = _get_day(d, area)
    except Exception as e:
        return json.dumps({"error": f"Could not load data: {e}"})

    needle = (name_contains or "").strip().lower()
    matches = []
    for r in day.rooms:
        cap = int(r.capacity) if (r.capacity and r.capacity.isdigit()) else None
        if min_capacity and (cap is None or cap < int(min_capacity)):
            continue
        if max_capacity and (cap is None or cap > int(max_capacity)):
            continue
        if needle and needle not in r.name.lower():
            continue
        ranges = _filter_ranges(ba._free_ranges(day.slots[r.id]), min_hours)
        if only_free and not ranges:
            continue
        matches.append({"room": r.name, "seats": r.capacity,
                        "free_blocks": [f"{s}-{e}" for s, e in ranges[:3]]})

    matches.sort(key=lambda m: -(int(m["seats"]) if (m["seats"] or "").isdigit() else 0))
    out = {
        "area": AREAS.get(area, area), "date": d,
        "match_count": len(matches),
        "rooms": matches[:10],
    }
    if stale:
        out["warning"] = "Live site unreachable, this is cached data."
    return json.dumps(out)


def tool_get_room_details(room: str, date: str = None, area: str = "3", min_hours: float = 0) -> str:
    return tool_check_availability(area=area, date=date, room=room, min_hours=min_hours)


def tool_book_room(room: str, date: str, time: str, title: str = "Study", area: str = "3") -> str:
    from mrbs import Mrbs, time_to_seconds, SLOT_MINUTES, SessionExpired, BookingError
    import booking_agent as ba

    area = _resolve_area(area)
    d = _norm_date(date)

    # Parse time
    try:
        s, e = [x.strip() for x in time.split("-")]
        ss, es = time_to_seconds(s), time_to_seconds(e)
    except ValueError:
        return json.dumps({"error": "Time must look like 14:00-16:00"})
    if es <= ss:
        return json.dumps({"error": "End time must be after start time"})

    cookies = ba.load_cookies()
    if not cookies:
        return json.dumps({"error": "NOT_SIGNED_IN",
                           "message": "The user needs to sign in first."})
    client = Mrbs(cookies=cookies)
    if not client.is_logged_in():
        return json.dumps({"error": "SESSION_EXPIRED",
                           "message": "The saved login expired. Sign in again."})

    # Re-check availability immediately before booking (concurrent safety).
    try:
        day = client.day_view(d, area)
    except Exception as ex:
        return json.dumps({"error": f"Could not check availability: {ex}"})
    r = day.room_by_key(room)
    if not r:
        return json.dumps({"error": f"Room '{room}' not found"})
    conflict = ba._first_conflict(day.slots[r.id], s, e)
    if conflict:
        return json.dumps({"error": f"{r.name} is already booked at {conflict}."})

    h, m = map(int, s.split(":"))
    try:
        form = client.get_booking_form(r.id, d, h, m, area=area)
    except SessionExpired:
        return json.dumps({"error": "SESSION_EXPIRED"})

    form["name"] = title
    form["description"] = title
    form["start_seconds"] = str(ss)
    form["end_seconds"] = str(
        es - SLOT_MINUTES * 60 if any("period" in k.lower() for k in form) else es)
    # Force the correct room in the form : the URL parameter pre-selects
    # it but some MRBS versions use a different default.
    if "rooms[]" in form:
        form["rooms[]"] = r.id
    if "area" in form:
        form["area"] = area
    # The live HKUST MRBS uses separate day/month/year fields rather
    # than a single start_date / end_date string. Set whichever
    # variant the form actually contains so the date takes effect.
    y, m, day = d.split("-")
    for prefix in ("start_", "end_"):
        if prefix + "date" in form:
            form[prefix + "date"] = d
        if prefix + "day" in form:
            form[prefix + "day"] = day
        if prefix + "month" in form:
            form[prefix + "month"] = m
        if prefix + "year" in form:
            form[prefix + "year"] = y
    form.setdefault("rep_type", "0")

    try:
        redirect_to = client.submit_booking(form)
    except SessionExpired:
        return json.dumps({"error": "SESSION_EXPIRED"})
    except BookingError as ex:
        return json.dumps({"error": f"Booking failed: {ex}"})

    import logging
    logging.getLogger("aria").info("booking submitted, redirected to: %s", redirect_to)

    _bust_cache(d, area)

    return json.dumps({"success": True, "room": r.name, "date": d,
                       "time": f"{s}-{e}", "title": title})


def tool_find_bookings(date: str = None, area: str = None, room: str = None,
                       search: str = None, mine: bool = False) -> str:
    """List bookings with entry ids so they can be cancelled.

    When date is omitted, checks today plus the next 6 days. When area is
    omitted, checks all bookable areas. Set mine=true to only return the
    current user's own bookings. Use search to filter by title text.
    """
    from mrbs import AREAS

    areas_to_check: list[str] = (
        [_resolve_area(area)] if area
        else [k for k in AREAS if k not in ("6",)]  # skip Teaching Venues
    )

    needle = (search or "").strip().lower()
    if mine and not needle:
        from booking_agent import load_cookies as _lc
        from mrbs import Mrbs as _Mrbs
        _cookies = _lc()
        if _cookies:
            try:
                _client = _Mrbs(cookies=_cookies)
                _form = _client.get_booking_form("57", _norm_date(None), 9, 0, area="8")
                # MRBS stores the ITSC username in create_by and the full
                # name in the name field. The booking title on the calendar
                # uses the full name. Filter by both so we catch all variants.
                parts = []
                for k in ("create_by", "name"):
                    v = (_form.get(k) or "").strip()
                    if v:
                        parts.append(v.lower())
                needle = "|".join(parts)  # "dsheng|sheng, dapeng"
            except Exception:
                # Identity lookup is best-effort (form structure may change,
                # network may be flaky). Auth failures (SessionExpired) are
                # raised by fetch_day when we scan dates below, so a silent
                # fallback here is safe — the user still gets all bookings.
                pass

    all_bookings: list[dict] = []
    errors = 0

    from mrbs import SLOT_MINUTES
    import booking_agent as ba

    # If no date given, scan today through +2 days (3 days total).
    # Scanning all 7 days × 7 areas = 49 HTTP calls against the slow
    # MRBS server takes ~60 s. Three days with 7 areas = 21 calls is
    # fast enough for a chat response while covering "current" bookings.
    dates_to_check: list[str] = []
    if date:
        dates_to_check = [_norm_date(date)]
    else:
        from datetime import date as _date, timedelta as _td
        today = _date.today()
        dates_to_check = [(today + _td(days=i)).strftime("%Y-%m-%d")
                          for i in range(3)]

    for d in dates_to_check:
        for a in areas_to_check:
            try:
                day, stale = _get_day(d, a)
            except Exception:
                errors += 1
                continue

            target = day.room_by_key(room) if room else None
            for r in day.rooms:
                if target and r.id != target.id:
                    continue
                current = None
                for slot in day.slots[r.id]:
                    if slot.status == "busy":
                        # MRBS only sets entry_id on the FIRST slot of a
                        # booking. Contiguous slots with the same label
                        # and no entry_id belong to the same booking.
                        same_booking = (
                            current
                            and current["title"] == slot.label
                            and (not slot.entry_id
                                 or current["entry_id"] == slot.entry_id)
                        )
                        if same_booking:
                            current["end"] = slot.time
                            if slot.entry_id:
                                current["entry_id"] = slot.entry_id
                        elif slot.entry_id:
                            current = {"entry_id": slot.entry_id,
                                       "room": r.name,
                                       "title": slot.label,
                                       "start": slot.time,
                                       "end": slot.time,
                                       "date": d,
                                       "area": AREAS.get(a, a)}
                            all_bookings.append(current)
                        # Slots with no entry_id AND no matching current
                        # are skipped (they're part of someone else's booking
                        # that started before our scan window, or system holds).
                    else:
                        current = None

    for b in all_bookings:
        b["time"] = f"{b.pop('start')}-{ba._add_minutes(b.pop('end'), SLOT_MINUTES)}"

    if needle:
        needles = needle.split("|")
        all_bookings = [
            b for b in all_bookings
            if any(n in (b.get("title") or "").lower() for n in needles)
        ]

    out = {"booking_count": len(all_bookings),
           "bookings": all_bookings[:20]}
    if errors:
        out["warnings"] = f"{errors} date/area combinations were unreachable."
    return json.dumps(out)


def tool_cancel_booking(entry_id: str) -> str:
    from mrbs import Mrbs, SessionExpired, BookingError
    import booking_agent as ba

    entry_id = str(entry_id or "").strip()
    if not entry_id.isdigit():
        return json.dumps({"error": "A numeric entry id is required. Use find_bookings first."})

    cookies = ba.load_cookies()
    if not cookies:
        return json.dumps({"error": "NOT_SIGNED_IN",
                           "message": "The user needs to sign in first."})
    client = Mrbs(cookies=cookies)
    if not client.is_logged_in():
        return json.dumps({"error": "SESSION_EXPIRED",
                           "message": "The saved login expired. Sign in again."})

    try:
        client.cancel_entry(entry_id)
    except SessionExpired:
        return json.dumps({"error": "SESSION_EXPIRED"})
    except BookingError as ex:
        return json.dumps({"error": f"Cancellation failed: {ex}"})
    except Exception as ex:
        return json.dumps({"error": f"Cancellation failed: {ex}"})

    _bust_cache()  # clear all cached day views after a cancellation

    return json.dumps({"success": True, "entry_id": entry_id})


TOOL_FUNCS = {
    "list_areas": tool_list_areas,
    "get_rooms_summary": tool_get_rooms_summary,
    "check_availability": tool_check_availability,
    "check_week": tool_check_week,
    "search_rooms": tool_search_rooms,
    "get_room_details": tool_get_room_details,
    "book_room": tool_book_room,
    "find_bookings": tool_find_bookings,
    "cancel_booking": tool_cancel_booking,
}

# Cap on stored conversation messages (system message excluded).
MAX_HISTORY_MESSAGES = 60


# ════════════════════════════════════════════════════════════════════
# The assistant
# ════════════════════════════════════════════════════════════════════
class VoiceAssistant:
    """LLM orchestration with tool calling. One instance can serve many
    requests: server.py keeps a singleton and guards turns with self.lock."""

    def __init__(self, config: dict):
        self.config = config
        self.lock = threading.Lock()
        self._llm = None
        self._model = ""
        self._history: list[dict] = []  # conversation messages
        self._ready = False

    # ---- setup -------------------------------------------------------
    def initialize(self) -> str | None:
        provider = self.config.get("api_provider", "")
        key = self.config.get("api_key", "")
        if not provider or not key:
            self._ready = False
            return "LLM: no API key (open Settings)"
        try:
            from openai import OpenAI
            info = cfg_mod.get_provider_info(provider) or cfg_mod.PROVIDERS["deepseek"]
            self._llm = OpenAI(api_key=key, base_url=info["base_url"])
            self._model = self.config.get("api_model") or info["default_model"]
        except Exception as e:
            self._ready = False
            return f"LLM: {e}"
        self._ready = True
        return None

    @property
    def is_ready(self) -> bool:
        return self._ready and self._llm is not None

    @property
    def model(self) -> str:
        return self._model

    def reset_history(self) -> None:
        self._history = []

    # ---- history helpers ----------------------------------------------
    def _ensure_system(self) -> None:
        if not self._history:
            today = date.today().strftime("%Y-%m-%d")
            content = SYSTEM_PROMPT.format(today=today)
            # Tell the model who is logged in so it never has to ask.
            identity = self._lookup_identity()
            if identity:
                content += f"\n\nThe signed-in user: {identity}."
            self._history.append({"role": "system", "content": content})

    @staticmethod
    def _lookup_identity() -> str:
        """Return the logged-in user's name and ITSC, or empty string."""
        try:
            from booking_agent import load_cookies
            from mrbs import Mrbs
            cookies = load_cookies()
            if not cookies:
                return ""
            client = Mrbs(cookies=cookies)
            if not client.is_logged_in():
                return ""
            today = date.today().strftime("%Y-%m-%d")
            form = client.get_booking_form("57", today, 9, 0, area="8")
            name = (form.get("name") or "").strip()
            create_by = (form.get("create_by") or "").strip()
            if name and create_by:
                return f"{name} (ITSC: {create_by})"
            return name or create_by
        except Exception:
            return ""

    def _trim_history(self) -> None:
        """Drop the oldest turns when history grows too long.

        Cuts only at user-message boundaries so assistant tool_calls are
        never separated from their tool results.
        """
        if len(self._history) <= MAX_HISTORY_MESSAGES:
            return
        # index 0 is the system message; find the first user message after
        # the region we want to drop and keep from there.
        keep_from = None
        surplus = len(self._history) - MAX_HISTORY_MESSAGES
        for i in range(1 + surplus, len(self._history)):
            if self._history[i].get("role") == "user":
                keep_from = i
                break
        if keep_from:
            self._history = [self._history[0]] + self._history[keep_from:]

    # ---- LLM with tools ------------------------------------------------
    def _stream_llm(self, messages):
        return self._llm.chat.completions.create(
            model=self._model, messages=messages,
            tools=TOOLS, tool_choice="auto",
            temperature=0.4, max_tokens=2000,
            stream=True,
        )

    def respond_stream(self, user_text: str):
        """Full agentic turn as a generator of event dicts.

        Yields, in order, any of:
          {"type": "token", "text": str}     - a piece of the reply
          {"type": "tool", "tool": str, "args": dict}
          {"type": "done", "message": str, "tool_events": list}
          {"type": "error", "message": str}
        """
        if not self._llm:
            yield {"type": "error",
                   "message": "I need an API key first. Open Settings and add one."}
            return

        # Fix common speech-to-text errors (e.g. "Elsie" -> "LC")
        user_text = _fix_speech(user_text)
        self._ensure_system()
        self._history.append({"role": "user", "content": user_text})
        self._trim_history()

        tool_events: list[dict] = []
        full_reply: list[str] = []

        # Agentic loop: up to 4 rounds of tool calls.
        # Any tool that errors counts as one strike, and strikes never
        # reset : a successful list_areas between two failing
        # availability checks doesn't mean the site healed. After two
        # failing tool calls total we stop the loop so the user sees a real
        # explanation instead of the unhelpful "stuck in a loop" message.
        tool_errors = 0
        for _round in range(4):
            if tool_errors >= 2:
                msg = ("The HKUST booking site requires login. "
                       "Open Settings, tap Connect Account to sign in "
                       "with your ITSC account, then try again.")
                yield {"type": "done", "message": msg,
                       "tool_events": tool_events}
                return
            try:
                stream = self._stream_llm(self._history)
            except Exception as e:
                yield {"type": "error",
                       "message": f"I hit an error talking to the AI: {e}"}
                return

            content_parts: list[str] = []
            calls: dict[int, dict] = {}  # index -> {id, name, arguments}
            try:
                for chunk in stream:
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    if delta is None:
                        continue
                    if delta.content:
                        content_parts.append(delta.content)
                        full_reply.append(delta.content)
                        yield {"type": "token", "text": delta.content}
                    for tc in (delta.tool_calls or []):
                        slot = calls.setdefault(tc.index, {
                            "id": "", "name": "", "arguments": ""})
                        if tc.id:
                            slot["id"] = tc.id
                        if tc.function:
                            if tc.function.name:
                                slot["name"] = tc.function.name
                            if tc.function.arguments:
                                slot["arguments"] += tc.function.arguments
            except Exception as e:
                yield {"type": "error",
                       "message": f"The AI stream was interrupted: {e}"}
                return

            content = "".join(content_parts)
            hist_entry: dict = {"role": "assistant", "content": content or None}
            if calls:
                hist_entry["tool_calls"] = [
                    {"id": c["id"], "type": "function",
                     "function": {"name": c["name"],
                                  "arguments": c["arguments"]}}
                    for _, c in sorted(calls.items())
                ]
            self._history.append(hist_entry)

            if not calls:
                message = "".join(full_reply).strip() or "Done."
                yield {"type": "done", "message": message,
                       "tool_events": tool_events}
                return

            # Execute tool calls
            any_error = False
            for _, c in sorted(calls.items()):
                name = c["name"]
                try:
                    args = json.loads(c["arguments"] or "{}")
                    if not isinstance(args, dict):
                        args = {}
                except json.JSONDecodeError:
                    args = {}
                tool_events.append({"tool": name, "args": args})
                yield {"type": "tool", "tool": name, "args": args}
                func = TOOL_FUNCS.get(name)
                if func is None:
                    result = json.dumps({"error": "unknown tool"})
                else:
                    try:
                        result = func(**args)
                        if isinstance(result, str) and result.strip().startswith('{"error"'):
                            any_error = True
                    except TypeError as e:
                        result = json.dumps({"error": f"Bad arguments: {e}"})
                    except Exception as e:
                        # Surface the actual error: if the HKUST site is
                        # behind a login wall, "Could not find the booking
                        # table" is the real problem, not a mystery to retry.
                        result = json.dumps({
                            "error": (
                                f"{e} : the booking site may require login "
                                f"or be unreachable. Do NOT retry this tool "
                                f"with different arguments."
                            ),
                        })
                self._history.append({
                    "role": "tool", "tool_call_id": c["id"], "content": result,
                })

            if any_error:
                tool_errors += 1

        yield {"type": "done",
               "message": "".join(full_reply).strip()
               or "I got stuck in a loop. Try rephrasing.",
               "tool_events": tool_events}

    def respond(self, user_text: str) -> dict:
        """Blocking turn built on respond_stream. Returns {message, tool_events}."""
        message = ""
        tool_events: list[dict] = []
        for event in self.respond_stream(user_text):
            if event["type"] == "done":
                message = event["message"]
                tool_events = event["tool_events"]
            elif event["type"] == "error":
                message = event["message"]
        return {"message": message, "tool_events": tool_events}

    # ---- Convenience ---------------------------------------------------
    def greeting(self) -> str:
        """What Aria says on startup."""
        return ("Hi, I'm Aria, your library booking assistant. "
                "Ask me which rooms are free, or tell me to book one for you.")
