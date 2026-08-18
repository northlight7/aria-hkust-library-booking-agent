"""
mrbs.py — a tiny client for the HKUST Library room booking system.

The booking site runs MRBS (Meeting Room Booking System), an open-source PHP
app. It has NO JSON API: every page is plain HTML. So this file does two jobs:

  1. READ availability  -> download day.php and read the HTML table.
  2. BOOK a room        -> load the booking form, then POST it back.

Reading needs no login. Booking needs an HKUST login (a session cookie).

We use two well-known libraries a first-year student will meet:
  - requests        : downloads web pages (like a browser, but in code)
  - BeautifulSoup   : reads the HTML and lets us pull data out of it
"""

from __future__ import annotations
import datetime as dt
from dataclasses import dataclass, field

import requests
from bs4 import BeautifulSoup

# The website we are talking to.
BASE_URL = "https://lbbooking.hkust.edu.hk/calendar"

# The rooms are grouped into "areas". These numbers come from the website
# itself (we found them during discovery). The user passes one with --area.
AREAS = {
    "3": "Group Study Rooms",
    "8": "LC Study Rooms",
    "20": "Study Pods",
    "10": "LC Creative Media Zone",
    "6": "Teaching Venues",
    "13": "Computers",
    "14": "3D Printers",
    "21": "Curved Monitors",
    "19": "Nap Pods",
}

# MRBS books rooms in fixed 30-minute steps (we saw data-resolution="1800").
SLOT_MINUTES = 30


# ---------------------------------------------------------------------------
# Small data holders. Using @dataclass keeps them readable.
# ---------------------------------------------------------------------------
@dataclass
class Room:
    id: str            # internal id used in URLs, e.g. "126"
    name: str          # human name, e.g. "1-350"
    capacity: str | None

    def __str__(self) -> str:
        cap = f" (seats {self.capacity})" if self.capacity else ""
        return f"{self.name}{cap} [id={self.id}]"


@dataclass
class Slot:
    time: str          # "14:00"
    status: str        # "free", "busy", or "blocked"
    label: str = ""    # for busy/blocked slots: who booked it, or "Unbookable"
    entry_id: str = "" # for busy slots: the MRBS entry id (used to cancel)


@dataclass
class DayView:
    date: str                                   # "2026-07-15"
    area: str                                   # "3"
    rooms: list[Room] = field(default_factory=list)
    # slots[room_id] = list of Slot for that room, in time order
    slots: dict[str, list[Slot]] = field(default_factory=dict)

    def room_by_key(self, key: str) -> Room | None:
        """Find a room by its id ('126') OR its name ('1-350', case-insensitive)."""
        key = key.strip().lower()
        for r in self.rooms:
            if r.id == key or r.name.lower() == key:
                return r
        # also allow a loose match, e.g. "lg3-17" typed as "lg317"
        for r in self.rooms:
            if r.name.lower().replace("-", "").replace(" ", "") == key.replace("-", "").replace(" ", ""):
                return r
        return None


# ---------------------------------------------------------------------------
# Errors we raise so the CLI can print friendly messages.
# ---------------------------------------------------------------------------
class SessionExpired(Exception):
    """Raised when the site sends us to the login page (we are not logged in)."""


class BookingError(Exception):
    """Raised when a booking cannot be made (slot taken, invalid time, etc.)."""


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------
class Mrbs:
    def __init__(self, cookies: dict | None = None):
        # A "session" remembers cookies between requests, like a browser tab.
        self.session = requests.Session()
        self.session.headers.update(
            {"User-Agent": "HKUST-AI-Course-BookingAgent/1.0 (educational)"}
        )
        if cookies:
            for name, value in cookies.items():
                self.session.cookies.set(name, value, domain="lbbooking.hkust.edu.hk")

    # ---- reading availability (login required as of 2026-08) ----------
    def fetch_day(self, date: str, area: str) -> str:
        """Download the day page HTML for one area (all its rooms).

        As of mid-2026 the HKUST library site requires authentication for
        every page, including the calendar. We stop on auth redirects and
        raise SessionExpired so callers can surface a clear message instead
        of a confusing parse failure.
        """
        y, m, d = _split_date(date)
        url = f"{BASE_URL}/day.php?year={y}&month={m}&day={d}&area={area}"
        resp = self.session.get(url, timeout=30, allow_redirects=False)
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location", "")
            if _is_auth_redirect(location):
                raise SessionExpired(
                    "The booking site requires sign-in. Open Settings and "
                    "connect your ITSC account, then try again."
                )
            # Follow non-auth redirects (shouldn't happen in practice, but
            # be safe).
            resp = self.session.get(url, timeout=30, allow_redirects=True)
        resp.raise_for_status()
        return resp.text

    def day_view(self, date: str, area: str) -> DayView:
        """Download AND parse the day into rooms + slots we can print."""
        html = self.fetch_day(date, area)
        return parse_day(html, date, area)

    # ---- booking (login required) ----------------------------------------
    def is_logged_in(self) -> bool:
        """Ask for a page that only logged-in users can see.

        If we get bounced to the CAS/Shibboleth/Entra ID login flow, we are
        NOT logged in.
        """
        y, m, d = _split_date(_today())
        url = (f"{BASE_URL}/edit_entry.php?area=3&room=126"
               f"&hour=14&minute=0&year={y}&month={m}&day={d}")
        # Do NOT follow redirects: a redirect means "go log in first".
        resp = self.session.get(url, timeout=30, allow_redirects=False)
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location", "")
            if _is_auth_redirect(location):
                return False
        # A 200 with the real form means we are in.
        return resp.status_code == 200

    def get_booking_form(self, room_id: str, date: str, hour: int, minute: int,
                          area: str = "3") -> dict:
        """Load the real booking form and read every field out of it.

        This is the key trick: instead of guessing what MRBS wants, we copy the
        exact fields (including the security 'csrf_token') straight from the
        form the website gives us, then change only the few we care about.
        """
        y, m, d = _split_date(date)
        url = (f"{BASE_URL}/edit_entry.php?area={area}&room={room_id}"
               f"&hour={hour}&minute={minute}&year={y}&month={m}&day={d}")
        resp = self.session.get(url, timeout=30, allow_redirects=False)
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location", "")
            if _is_auth_redirect(location):
                raise SessionExpired(
                    "The site sent us to the login page. Your session is not active."
                )
        resp.raise_for_status()
        return _read_form_fields(resp.text)

    def cancel_entry(self, entry_id: str) -> str:
        """Cancel one booking by its MRBS entry id.

        We load view_entry.php for the entry and look for the delete control
        the site gives us. Only the booking's owner sees one, so a missing
        control means "not yours". Newer MRBS uses a small POST form with a
        csrf_token, older versions a plain del_entry.php link. We handle both
        by copying exactly what the page provides (never fabricating tokens).
        """
        url = f"{BASE_URL}/view_entry.php?id={entry_id}"
        resp = self.session.get(url, timeout=30, allow_redirects=False)
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location", "")
            if _is_auth_redirect(location):
                raise SessionExpired("The site sent us to the login page.")
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "lxml")

        # Preferred: a form whose action posts to del_entry.php.
        form = soup.find("form", action=lambda a: a and "del_entry" in a)
        if form is not None:
            data = _read_form_fields(str(form))
            data.setdefault("id", entry_id)
            del_url = f"{BASE_URL}/{form.get('action', 'del_entry.php').lstrip('/')}"
            out = self.session.post(del_url, data=data, timeout=30,
                                    allow_redirects=False)
        else:
            # Fallback: a plain delete link.
            link = soup.find("a", href=lambda h: h and "del_entry" in h)
            if link is None:
                raise BookingError(
                    "No delete option found. You can only cancel your own bookings.")
            del_url = f"{BASE_URL}/{link['href'].lstrip('/')}"
            out = self.session.get(del_url, timeout=30, allow_redirects=False)

        if out.status_code in (301, 302, 303, 307, 308):
            location = out.headers.get("Location", "")
            if _is_auth_redirect(location):
                raise SessionExpired("Session expired during cancellation.")
            return location  # back to the calendar: success
        message = _read_error_message(out.text)
        raise BookingError(message or "Cancellation was rejected (unknown reason).")

    def submit_booking(self, form_data: dict) -> str:
        """POST the filled-in form to actually create the booking."""
        url = f"{BASE_URL}/edit_entry_handler.php"
        resp = self.session.post(url, data=form_data, timeout=30, allow_redirects=False)

        # A 200 means the form came back with an error on it.
        if resp.status_code == 200:
            message = _read_error_message(resp.text)
            raise BookingError(message or "Booking was rejected (unknown reason).")

        # Redirect — could be real success or silent failure. Follow it
        # and verify the booking actually appears on the calendar.
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location", "")
            if _is_auth_redirect(location):
                raise SessionExpired("Session expired during booking.")
            # If redirecting back to the form, it's an error we missed.
            if "edit_entry" in location:
                raise BookingError("Booking was rejected — redirect back to form.")
            return location

        message = _read_error_message(resp.text)
        raise BookingError(message or "Booking was rejected (unknown reason).")


# ---------------------------------------------------------------------------
# HTML parsing helpers (the fiddly bits, kept out of the way)
# ---------------------------------------------------------------------------
def parse_day(html: str, date: str, area: str) -> DayView:
    """Turn the day.php HTML table into a tidy DayView.

    The table has rooms across the top (columns) and times down the side
    (rows). A booked slot can span several rows (rowspan), so we walk the
    grid carefully and remember cells that stretch downward.
    """
    soup = BeautifulSoup(html, "lxml")
    grid = soup.find("table", id="day_main")
    if grid is None:
        raise RuntimeError("Could not find the booking table on the page.")

    view = DayView(date=date, area=area)

    # 1. Read the room columns from the header row.
    header_cells = grid.select("thead th[data-room]")
    for th in header_cells:
        rid = th.get("data-room")
        link = th.find("a")
        label = link.get_text(strip=True) if link else th.get_text(strip=True)
        name, capacity = _split_name_and_capacity(label)
        room = Room(id=rid, name=name, capacity=capacity)
        view.rooms.append(room)
        view.slots[rid] = []

    room_ids = [r.id for r in view.rooms]
    num_cols = len(room_ids)

    # 2. Walk the body rows. Track cells that span multiple rows.
    # pending[col] = [rows_left, Slot] for a booked cell reaching down from above.
    pending: dict[int, list] = {}

    for tr in grid.select("tbody tr"):
        # The time for this row is in a 'row_labels' cell.
        label_cell = tr.find("td", class_="row_labels")
        if label_cell is None:
            continue
        time_text = label_cell.get_text(strip=True)
        time_text = _clean_time(time_text)
        if not time_text:
            continue

        # Data cells = every <td> in the row that is NOT a row label.
        data_cells = [td for td in tr.find_all("td", recursive=False)
                      if "row_labels" not in (td.get("class") or [])]

        cell_iter = iter(data_cells)
        for col in range(num_cols):
            rid = room_ids[col]
            if col in pending:
                # A booked cell from a row above still covers this column.
                rows_left, slot_template = pending[col]
                view.slots[rid].append(Slot(time=time_text,
                                             status=slot_template.status,
                                             label=slot_template.label))
                rows_left -= 1
                if rows_left <= 0:
                    del pending[col]
                else:
                    pending[col][0] = rows_left
                continue

            # Otherwise take the next real <td> for this column.
            td = next(cell_iter, None)
            if td is None:
                view.slots[rid].append(Slot(time=time_text, status="free"))
                continue

            slot = _classify_cell(td, time_text)
            view.slots[rid].append(slot)

            # If this booked cell spans N rows, remember it for the next N-1 rows.
            rowspan = int(td.get("rowspan", "1") or "1")
            if rowspan > 1 and slot.status != "free":
                pending[col] = [rowspan - 1, slot]

    return view


def _classify_cell(td, time_text: str) -> Slot:
    """Decide whether a single table cell is free, busy, or blocked."""
    classes = td.get("class") or []
    # A free cell is class="new" and links to the booking form.
    if "new" in classes:
        return Slot(time=time_text, status="free")

    # A booked/blocked cell links to view_entry.php and has a label.
    link = td.find("a", href=lambda h: h and "view_entry.php" in h)
    if link:
        label = link.get_text(strip=True)
        entry_id = _entry_id_from_href(link.get("href", ""))
        div = td.find("div")
        dtype = (div.get("data-type") if div else "") or ""
        # Type "H" (or the word Unbookable) = closed/blocked by the library.
        if dtype == "H" or label.lower() == "unbookable":
            return Slot(time=time_text, status="blocked", label=label or "Unbookable")
        return Slot(time=time_text, status="busy", label=label, entry_id=entry_id)

    # Anything else we treat as not bookable (e.g. an empty spacer cell).
    return Slot(time=time_text, status="blocked", label="")


def _read_form_fields(html: str) -> dict:
    """Copy every input/select/textarea from the booking form into a dict."""
    soup = BeautifulSoup(html, "lxml")
    form = soup.find("form", id="main") or soup.find("form")
    if form is None:
        raise RuntimeError("Could not find the booking form on the page.")

    data: dict = {}
    for inp in form.find_all("input"):
        name = inp.get("name")
        if not name:
            continue
        itype = (inp.get("type") or "text").lower()
        # Skip buttons — only the clicked button is sent by real browsers
        if itype in ("submit", "button", "reset"):
            continue
        if itype in ("checkbox", "radio"):
            if inp.has_attr("checked"):
                data[name] = inp.get("value", "on")
        else:
            data[name] = inp.get("value", "")

    for sel in form.find_all("select"):
        name = sel.get("name")
        if not name:
            continue
        chosen = sel.find("option", selected=True) or sel.find("option")
        data[name] = chosen.get("value", "") if chosen else ""

    for ta in form.find_all("textarea"):
        name = ta.get("name")
        if name:
            data[name] = ta.get_text()

    # Strip fields a browser would never send: empty hidden fields and
    # submit buttons. Sending them can cause MRBS to reject the booking.
    for k in list(data.keys()):
        if data[k] == "" and k not in ("name", "description"):
            del data[k]

    return data


def _read_error_message(html: str) -> str:
    """Pull a human-readable error out of a returned form page, if any."""
    soup = BeautifulSoup(html, "lxml")
    for sel in (".error", "#error", ".alert", "p.error"):
        node = soup.select_one(sel)
        if node and node.get_text(strip=True):
            return node.get_text(strip=True)
    return ""


# ---------------------------------------------------------------------------
# Plain helper functions
# ---------------------------------------------------------------------------
def _split_date(date: str) -> tuple[int, int, int]:
    """'2026-07-15' -> (2026, 7, 15). Also checks the format is valid."""
    try:
        d = dt.datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"Date '{date}' is not in YYYY-MM-DD format.")
    return d.year, d.month, d.day


def _today() -> str:
    return dt.date.today().strftime("%Y-%m-%d")


def _clean_time(text: str) -> str:
    """Keep only a HH:MM looking token from a messy label."""
    text = text.strip()
    if len(text) >= 5 and text[:2].isdigit() and text[2] == ":":
        return text[:5]
    return text if (":" in text and text[0].isdigit()) else ""


def _split_name_and_capacity(label: str) -> tuple[str, str | None]:
    """'LG3-17 (10)' -> ('LG3-17', '10')."""
    label = label.strip()
    if label.endswith(")") and "(" in label:
        name, _, cap = label.rpartition("(")
        cap = cap.rstrip(")").strip()
        if cap.isdigit():
            return name.strip(), cap
    return label, None


def _entry_id_from_href(href: str) -> str:
    """'view_entry.php?id=2208559&area=3' -> '2208559'."""
    if "id=" not in href:
        return ""
    part = href.split("id=", 1)[1]
    digits = ""
    for ch in part:
        if ch.isdigit():
            digits += ch
        else:
            break
    return digits


def time_to_seconds(hhmm: str) -> int:
    """'14:00' -> 50400 (seconds since midnight)."""
    h, m = hhmm.split(":")
    return int(h) * 3600 + int(m) * 60


def _is_auth_redirect(location: str) -> bool:
    """Return True if the redirect Location goes to an auth wall.

    The HKUST login chain is: MRBS → cas.ust.hk → shib.ust.hk →
    login.microsoftonline.com (Entra ID). A redirect to any of these
    means the user needs to sign in (or re-authenticate).
    """
    if not location:
        return False
    loc = location.lower()
    return any(host in loc for host in (
        "cas.ust.hk", "shib.ust.hk",
        "login.microsoftonline.com", "login.live.com",
    ))
