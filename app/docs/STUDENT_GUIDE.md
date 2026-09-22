# Student Guide — Aria, Your Library Booking Assistant

*HKUST AI Literacy Course · Path A: Agentic AI Exemplar*

> **Windows:** download `Library_Booking_Agent.zip`, extract, double-click `LAUNCHER.bat`.
> **macOS:** download `Library_Booking_Agent.tar.gz`, extract, double-click `LAUNCHER.command`. No Terminal needed.

---

## What Aria does

Aria is a **voice assistant** whose only job is helping you check and book
HKUST library rooms. You talk, she acts:

- "How many rooms can I book?" → totals + biggest/smallest room
- "Any empty Learning Commons rooms tomorrow at 3 pm?" → live availability
- "What's the largest room?" → capacity lookup
- "Book 1-350 from 2 to 4 pm for my study group." → she books it

She answers out loud and shows the conversation on screen. No coding needed.

---

## Setup (5 minutes)

### 1. Extract & launch

**Windows:** download the `.zip`, extract anywhere, double-click `LAUNCHER.bat`.

**macOS (one-time setup, 15 seconds):**
1. Download the `.tar.gz` and double-click to extract.
2. Open **Terminal** (Applications → Utilities), paste this line, press Enter:
   `xattr -dr com.apple.quarantine ~/Desktop/Library_Booking_Agent`
   (If you extracted somewhere else, drag the `Library_Booking_Agent` folder onto
   the Terminal window instead of typing the path — it fills in automatically.)
3. That's it. Double-click `LAUNCHER.command` — it works now and every time after.

### 2. Give Aria a brain (API key)
On first launch she asks for an AI API key. Get one free:
- **DeepSeek** (cheapest): platform.deepseek.com → API Keys
- **OpenRouter** (many models): openrouter.ai → Keys

Paste it into the dialog and click **Save & Continue**. Aria greets you by voice.
Your key is stored locally (`config.json`) and changeable via the ⚙ icon.

### 3. Sign in (only to book)
Both checking and booking need your HKUST login. The library site sends
anyone who is not signed in to the CAS login page, so Aria cannot read the
day page until you connect your account:
1. Click the 🔒 icon.
2. Sign in with your ITSC account in the browser that opens (+ 2FA).
3. Click **I'm Done** when the calendar shows.

Your password is never stored — only a session cookie (`auth_state.json`).

---

## Using Aria

### Talk or type
- Tap the glowing **orb** and speak, **or** type in the bottom bar.
- The orb changes color: **blue** idle · **cyan** listening · **violet** thinking · **green** speaking.

### What to say
| You say | Aria does |
|---|---|
| "How many rooms are there?" | Totals + which is biggest |
| "What's free tomorrow?" | Group Study Rooms availability |
| "Any nap pods free?" | Checks Nap Pods |
| "Show 1-350's schedule today" | Exact free blocks for that room |
| "Book 1-350 from 2 to 4 pm for our meeting" | Books it (after sign-in) |
| "Which room seats the most people?" | Capacity lookup |

If she's unsure, she asks a short follow-up instead of guessing.

### Manual panel (optional)
Click the 🎛 icon for dropdowns (Area, Room, Date, Time, Title) and Check/Book
buttons — but voice is the main way.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| "Add an API key to wake Aria up" | Open ⚙ and paste a valid key |
| Aria doesn't hear you | Check mic is plugged in & allowed (OS privacy settings) |
| "I didn't catch that" | Speak louder/closer, try again |
| "You need to sign in first" | Click 🔒 and sign in |
| "Session expired" | Sign in again via 🔒 |
| App won't open | Install Python 3.10+ from python.org (tick "Add to PATH") |
| Booked the wrong room | Cancel at lbbooking.hkust.edu.hk/calendar (Aria can't cancel) |

---

## Privacy

- **Password:** never stored — only a temporary login cookie.
- **API key:** stays on your computer.
- **Voice audio:** goes to Google (transcribe) + your AI provider (think). No recordings kept.
- **Room data:** from the public library calendar; stays on your machine.

See `RESPONSIBLE_AI.md` for the full analysis.

---

## Quick reference

```
LAUNCHER.bat (Win) / LAUNCHER.command (Mac)
  → first run: paste API key ("Wake up Aria")
  → tap orb (or type) → ask / book
  → 🔒 sign in (to book)   ⚙ settings   🎛 manual panel
```
