# Aria

A voice-first AI assistant that checks and books HKUST library rooms.

## How it works

Talk to Aria naturally. She transcribes your words, runs them through an AI
with tools, and can query the live library booking system for room
availability and details, or book a slot for you. Her voice is a free offline
neural voice that runs locally on your machine.

## Where the data comes from

She reads the HKUST library booking site directly. That site runs on MRBS,
which has no JSON API, so the HTML itself is the API: Aria parses the day
page for free and booked rooms, and posts the real booking form to book.
Checking is open; booking needs a one-time HKUST sign-in. Your words go only
to the AI provider you choose.

## Run it

The launchers install everything they need into this folder. No admin
password, no system Python, no Node. The first run asks for an API key and
downloads the offline voice.

- **macOS** — double-click `LAUNCHER.command`, or run `./LAUNCHER.sh`
- **Windows** — double-click `LAUNCHER.bat`

Then open <http://localhost:8091>.

## Privacy

Your password is never stored, only a login cookie. Your API key stays in
`config.json` on your computer. Voice is synthesized locally.
