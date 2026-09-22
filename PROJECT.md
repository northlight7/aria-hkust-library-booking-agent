# Aria: HKUST Library Booking Assistant

## What this is
A voice-first AI assistant that checks and books HKUST library study rooms, built
for the HKUST AI Literacy Course (Path A, agentic AI exemplar). Students run it
locally from one double-clickable launcher. Repo:
https://github.com/northlight7/aria-hkust-library-booking-agent

## Status
Working on macOS and verified end to end on 2026-09-22: live room data, real
tool-calling conversation, and local neural voice all confirmed. A serious
network-exposure bug and three Windows faults were fixed the same day and pushed.
Not yet re-tested on a real Windows machine.

## Current progress
- [x] Engine (MRBS HTML parsing), Playwright CAS sign-in, 9 agent tools, Kokoro voice
- [x] 14/14 tests pass (`uv run pytest` in `app/`)
- [x] Fixed: server bound 0.0.0.0 with Flask debug ON (see Decisions) — now 127.0.0.1, debug off
- [x] Fixed: onnxruntime pinned to 1.23.2 on every platform, which has no win_arm64
      wheel, so `uv sync` failed on ARM Windows. Resolution now forks.
- [x] Fixed: LAUNCHER.bat LF line endings, plus `.gitattributes` with `*.bat -text`
- [x] Fixed: sign-in hint said "Cmd+Tab / Dock" on Windows; now Alt+Tab / taskbar
- [x] Fixed: both launchers now keep the uv cache inside `.uv/` (385 MB was escaping)
- [x] Corrected the docs: checking rooms now needs sign-in too (see Notes)
- [x] `Aria - How To Build It.docx` written, 17 pages, 15 copyable prompts
- [ ] Re-test on a real Windows machine, ideally one ARM and one x86
- [ ] `Aria - Tutorial.docx` (the setup/use guide, July) is now partly STALE: it
      still says checking rooms works without signing in. Refresh or retire it.

## Decisions
- **Bind 127.0.0.1, never 0.0.0.0.** The server used to listen on every network
  interface with Flask's debug mode on by default. Verified before the fix: the
  page and `/api/session` answered 200 from this machine's LAN address. Aria holds
  the user's HKUST session, so anyone on the same campus Wi-Fi could have booked
  rooms in their name. Nothing needs the wider bind, and the browser only grants
  microphone access on localhost anyway. `ARIA_HOST` / `ARIA_DEBUG` override.
- **The HTML is the API.** MRBS publishes no JSON, so `mrbs.py` parses the day page
  and posts the real booking form. Fragile by nature: a site redesign breaks it, so
  parsing failures must raise, never return an empty list that reads as "nothing free".
- **Never hold the password.** Playwright opens a real browser, the user signs in
  through CAS with 2FA themselves, and only the resulting cookies are kept in
  `auth_state.json`. Deleting that file is what logging out means.
- **The voice runs locally.** Kokoro (82M params, ONNX, Apache-2.0), ~337 MB once.
  Free, offline, nothing spoken leaves the laptop. Falls back to the browser voice
  while the model is still downloading, then switches over without a reload.
- **Confirm before acting, not after.** Reading is free; booking a room the user
  named exactly is allowed; booking a room Aria chose, cancelling, or anything
  ambiguous must be confirmed first.

## Notes
- Run it: double-click `LAUNCHER.command` (macOS) or `LAUNCHER.bat` (Windows), then
  open http://localhost:8091.
- **Both checking and booking now require HKUST sign-in.** The library site began
  redirecting anonymous visitors to cas.ust.hk, so even the day page is behind CAS.
  Aria detects the redirect and says so. This changed after the project was built.
- Secrets: `app/config.json` (API key) and `app/auth_state.json` (session cookies)
  are gitignored and have never been committed — verified against every blob in
  history and against the published ZIP. `/api/settings` returns only `has_key`.
- `.gitattributes` forces the real CRLF bytes into `LAUNCHER.bat` with `-text`.
  Do not change it to `text eol=crlf`: that normalises the blob to LF and leaves the
  line endings up to whoever generates the download.
- Tutorial working files are in `.internal/` (gitignored): `build_howto.py`
  regenerates the docx from `.internal/shots/`.
- The original build brief lives at `../.internal/PROMPT.md`.
