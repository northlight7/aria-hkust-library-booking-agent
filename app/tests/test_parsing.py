"""
Offline tests for the booking agent's parsing and logic.

These run WITHOUT the network: they parse a saved copy of a real calendar page
(tests/sample_day.html) and the sample booking form. Run either way:

    python tests/test_parsing.py       # plain, no extra tools
    pytest                             # if you have pytest installed
"""
import os
import sys

# Make the project importable from app/.
HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(HERE)  # app/ directory
sys.path.insert(0, APP_DIR)

import mrbs
import booking_agent as ba
from mrbs import Slot, DayView, Room
import contextlib
import io


@contextlib.contextmanager
def _quiet():
    """Swallow the agent's print() output so test runs stay readable."""
    with contextlib.redirect_stdout(io.StringIO()):
        yield

SAMPLE_DAY = os.path.join(HERE, "sample_day.html")
SAMPLE_FORM = os.path.join(APP_DIR, "assets", "sample_edit_form.html")


def _day():
    with open(SAMPLE_DAY, encoding="utf-8", errors="replace") as f:
        return mrbs.parse_day(f.read(), "2026-07-15", "3")


# --- parsing the day grid --------------------------------------------------
def test_finds_all_rooms():
    assert len(_day().rooms) == 37


def test_room_lookup_by_id_and_name():
    day = _day()
    assert day.room_by_key("126").name == "1-350"
    assert day.room_by_key("LG3-17").id == "15"
    assert day.room_by_key("lg317").id == "15"      # loose match


def test_rowspan_booking_is_tracked():
    # LG1-355 (id 37) has a real multi-slot booking from 09:30 in the sample.
    # If rowspan tracking is wrong, later rows shift and this misaligns.
    slots = {s.time: s for s in _day().slots["37"]}
    assert slots["09:00"].status == "free"
    assert slots["09:30"].status == "busy"
    assert slots["10:00"].status == "busy"          # span held across rows


def test_blocked_slots_detected():
    slots = {s.time: s for s in _day().slots["126"]}
    assert slots["14:00"].status == "free"
    assert slots["21:00"].status == "blocked"


# --- reading the booking form ----------------------------------------------
def test_form_fields_extracted():
    with open(SAMPLE_FORM, encoding="utf-8") as f:
        form = mrbs._read_form_fields(f.read())
    assert form["csrf_token"].startswith("SAMPLE")
    assert form["rooms[]"] == "126"
    assert form["type"] == "T"
    assert "all_day" not in form                    # unchecked box is omitted


# --- pure logic helpers ----------------------------------------------------
def test_time_maths():
    assert mrbs.time_to_seconds("14:00") == 50400
    assert ba._times_in_range("14:00", "15:00") == ["14:00", "14:30"]
    assert ba._add_minutes("23:30", 30) == "00:00"


def test_conflict_and_free_ranges():
    slots = [Slot("14:00", "free"), Slot("14:30", "free"),
             Slot("15:00", "busy", "X"), Slot("15:30", "busy", "X"),
             Slot("16:00", "free")]
    assert ba._first_conflict(slots, "14:00", "15:00") is None
    assert ba._first_conflict(slots, "14:30", "16:00") == "15:00"
    assert ba._free_ranges(slots) == [("14:00", "15:00"), ("16:00", "16:30")]


# --- mock booking flow (no network, no real booking) -----------------------
def _patch_day_view():
    def fake(self, date, area):
        dv = DayView(date=date, area=area)
        dv.rooms = [Room(id="126", name="1-350", capacity="8")]
        dv.slots["126"] = [Slot("14:00", "free"), Slot("14:30", "free"),
                           Slot("15:00", "busy", "Someone")]
        return dv
    mrbs.Mrbs.day_view = fake


class _Args:
    date = "2026-07-15"; area = "3"; room = "126"; title = "Group meeting"


def test_book_mock_free_slot_succeeds():
    _patch_day_view()
    with _quiet():
        assert ba._book_mock(_Args(), "14:00", "15:00", 50400, 54000) == 0


def test_book_mock_busy_slot_stops():
    _patch_day_view()
    with _quiet():
        assert ba._book_mock(_Args(), "14:30", "15:30", 52200, 55800) == 1


# --- entry ids (needed for cancellation) -----------------------------------
def test_entry_ids_parsed_from_busy_slots():
    day = _day()
    busy = [s for slots in day.slots.values() for s in slots if s.status == "busy"]
    with_ids = [s for s in busy if s.entry_id]
    assert with_ids, "busy slots should carry an MRBS entry id"
    assert all(s.entry_id.isdigit() for s in with_ids)


def test_entry_id_from_href():
    assert mrbs._entry_id_from_href("view_entry.php?id=2208559&area=3") == "2208559"
    assert mrbs._entry_id_from_href("view_entry.php?area=3") == ""


# --- assistant tool logic (offline) ----------------------------------------
def test_filter_ranges_by_min_hours():
    import assistant as A
    ranges = [("14:00", "15:00"), ("15:00", "17:30")]
    assert A._filter_ranges(ranges, 2) == [("15:00", "17:30")]
    assert A._filter_ranges(ranges, 0) == ranges


def test_find_bookings_groups_contiguous_slots():
    import json as _json
    import assistant as A
    dv = DayView(date="2026-07-15", area="3")
    dv.rooms = [Room(id="126", name="1-350", capacity="8")]
    dv.slots["126"] = [
        Slot("14:00", "busy", "Group meeting", "111"),
        Slot("14:30", "busy", "Group meeting", "111"),
        Slot("15:00", "free"),
        Slot("15:30", "busy", "Other booking", "222"),
    ]
    original = A._get_day
    A._get_day = lambda d, a: (dv, False)
    try:
        out = _json.loads(A.tool_find_bookings(date="2026-07-15", area="3"))
        filtered = _json.loads(A.tool_find_bookings(date="2026-07-15",
                                                    search="group", area="3"))
    finally:
        A._get_day = original
    assert out["booking_count"] == 2
    first = out["bookings"][0]
    assert first["entry_id"] == "111"
    assert first["time"] == "14:00-15:00"
    assert filtered["booking_count"] == 1
    assert filtered["bookings"][0]["entry_id"] == "111"


def test_check_week_counts_free_rooms_per_day():
    import json as _json
    import assistant as A
    dv = DayView(date="x", area="3")
    dv.rooms = [Room(id="126", name="1-350", capacity="8")]
    dv.slots["126"] = [Slot("14:00", "free"), Slot("14:30", "free")]
    original = A._get_day
    A._get_day = lambda d, a: (dv, False)
    try:
        out = _json.loads(A.tool_check_week(start_date="2026-07-15", days=3))
    finally:
        A._get_day = original
    assert len(out["days"]) == 3
    assert out["days"][0] == {"date": "2026-07-15", "free_room_count": 1}
    assert out["days"][2]["date"] == "2026-07-17"


# --- run as a plain script -------------------------------------------------
if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
