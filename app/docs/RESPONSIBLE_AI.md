# Responsible AI Analysis

**Stale data and double-booking.** Our agent reads a *snapshot* of the calendar,
but rooms can be taken in the seconds between reading and booking. If the agent
trusted old data it could grab a slot someone else just took, or overwrite a
booking. We reduce this by re-checking the exact slot immediately before
submitting, and by letting the server — the single source of truth — make the
final decision and reject clashes. The agent never assumes success; it confirms
what the server actually did.

**Hoarding.** A script can book far faster than a person, so it could be abused
to reserve many rooms and deny them to others. That would be unfair and likely
breaks library rules. An exemplar agent should book only what a real user needs,
never bulk-reserve, and respect the library's own booking limits.

**Data handling.** The agent handles a login session cookie (stored locally in
`auth_state.json`, not a password) and booking details. It also *sees other
students' names*, which the public calendar displays next to bookings. That data
stays on the user's own machine and is sent only to the official HKUST site —
nowhere else. Names we see should never be logged, shared, or reused.
