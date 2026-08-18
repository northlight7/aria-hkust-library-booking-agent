"""
tts.py — Local, free, offline neural text-to-speech using Kokoro.

Kokoro-82M (https://github.com/thewh1teagle/kokoro-onnx, Apache-2.0) runs an
82M-parameter neural voice entirely on this machine through ONNX Runtime. No
account, no API key, no per-character cost, no network call once the model is
cached. It is a clear step up from the classic system voices the browser's
Web Speech API can reach (on macOS without a downloaded Premium/Enhanced
voice pack, that is Samantha, which sounds robotic), and from the smaller
Piper voice this module used previously.

The model (~310MB) plus its voice bank (~27MB) download once in the
background and are cached in voices/. Synthesis after that is local and runs
several times faster than real time. While the download is in progress,
/api/tts returns 503 and the frontend falls back to the browser's own
speechSynthesis voice for that turn.
"""

from __future__ import annotations

import io
import logging
import os
import re
import threading
import wave

import numpy as np
import requests

logger = logging.getLogger("aria.tts")

# af_heart: the warmest and most natural of Kokoro's American female voices,
# chosen by ear against af_bella and bf_emma. Kokoro ships 54 voices; swap
# this for any of them (k.get_voices()) without touching anything else.
VOICE_NAME = "af_heart"

# Kokoro's own default pacing already reads at a natural conversational
# speed, unlike Piper, which needed to be sped up. 1.0 means "as trained".
SPEAKING_RATE = 1.0

LANG = "en-us"

# Kokoro phonemizes through espeak-ng. Several things read wrong without
# preprocessing, applied in this order so each step feeds clean input to
# the next.

# (1) All-caps runs like HKUST: espeak treats them as a made-up word instead
#     of an acronym. Spacing the letters makes espeak read each by name.
_ACRONYM_RE = re.compile(r"\b[A-Z]{2,6}\b")

# (2a) 24-hour time ranges: "14:00-16:00" -> "14:00 to 16:00"
_H24_RANGE_RE = re.compile(r"(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})")

# (2b) Bare hour ranges: "6-8" or "2 - 4" (but not dates or room names).
_TIME_RANGE_RE = re.compile(
    r"\b([1-9]|1\d|2[0-3])\s*-\s*([1-9]|1\d|2[0-3])\b")

# (3) AM / PM: Kokoro reads "AM" as the word "am," not "A M." Spread the
#     letters so espeak reads them by name. Handles "6PM", "6 PM", "6:30pm".
_AMPM_RE = re.compile(r"\b(\d{1,2}(?::\d{2})?)\s*([AaPp]\.?[Mm]\.?)\b")

# (4) Bare AM/PM without a preceding number (rare but possible).
_BARE_AMPM_RE = re.compile(r"\b([AaPp]\.?[Mm]\.?)\b")

# (5) 24-hour times: "18:00" -> "6 PM", "09:30" -> "9 30 AM"
_H24_RE = re.compile(r"\b(\d{1,2}):(\d{2})\b")


def _convert_24h(m):
    h = int(m.group(1))
    mm = m.group(2)
    suffix = "A M" if h < 12 else "P M"
    h12 = h % 12 or 12
    if mm == "00":
        return f"{h12} {suffix}"
    return f"{h12} {mm} {suffix}"


def _preprocess_for_speech(text: str) -> str:
    """Make text pronounceable by Kokoro/espeak-ng."""
    # Acronyms: HKUST -> H K U S T
    text = _ACRONYM_RE.sub(lambda m: " ".join(m.group(0)), text)
    # Time ranges FIRST: "14:00-16:00" -> "14:00 to 16:00", "6-8" -> "6 to 8"
    text = _H24_RANGE_RE.sub(r"\1 to \2", text)
    text = _TIME_RANGE_RE.sub(r"\1 to \2", text)
    # Then 24-hour times: "18:00" -> "6 PM", "09:30" -> "9 30 AM"
    text = _H24_RE.sub(_convert_24h, text)
    # AM/PM after a time: 6 PM -> 6 P M, 6:30pm -> 6 30 P M
    def _spread_ampm(m):
        time_part = m.group(1).replace(":", " ")
        ampm = " ".join(c.upper() for c in m.group(2) if c.isalpha())
        return f"{time_part} {ampm}"
    text = _AMPM_RE.sub(_spread_ampm, text)
    # Bare AM/PM: "AM" -> "A M"
    text = _BARE_AMPM_RE.sub(
        lambda m: " ".join(c.upper() for c in m.group(1) if c.isalpha()),
        text)
    return text


def _spell_out_acronyms(text: str) -> str:
    # Kept for backwards compat; _preprocess_for_speech handles this now.
    return _preprocess_for_speech(text)


HERE = os.path.dirname(os.path.abspath(__file__))
VOICES_DIR = os.path.join(HERE, "voices")

MODEL_FILENAME = "kokoro-v1.0.onnx"
VOICES_FILENAME = "voices-v1.0.bin"
_RELEASE_URL = ("https://github.com/thewh1teagle/kokoro-onnx/releases/"
                "download/model-files-v1.0/")

# Exact sizes from the public release, used to catch a truncated or otherwise
# corrupt download immediately rather than only discovering it when ONNX
# Runtime later fails to load the file.
EXPECTED_MODEL_SIZE = 325532387
EXPECTED_VOICES_SIZE = 28214398

_voice = None
_voice_lock = threading.Lock()
_download_started = False
_download_lock = threading.Lock()
_ready_event = threading.Event()
_last_error: str | None = None


def _model_path() -> str:
    return os.path.join(VOICES_DIR, MODEL_FILENAME)


def _voices_path() -> str:
    return os.path.join(VOICES_DIR, VOICES_FILENAME)


def _files_present() -> bool:
    return os.path.exists(_model_path()) and os.path.exists(_voices_path())


def _remove_cached_files() -> None:
    """Delete whatever's cached so the next attempt starts clean instead of
    forever retrying a file that's already known to be bad."""
    for path in (_model_path(), _voices_path()):
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass


def _download_file(url: str, dest: str, expected_size: int | None = None) -> None:
    """Stream a file to disk, writing to a .part file first so a crash or
    dropped connection never leaves a corrupt model file at the final path.
    Also checked against its known size: some proxies/CDNs return a
    200 with truncated or substitute content instead of a clean failure,
    which downloads fine but produces a file ONNX Runtime can't load."""
    tmp = dest + ".part"
    with requests.get(url, stream=True, timeout=60) as resp:
        resp.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1 << 20):
                if chunk:
                    f.write(chunk)
    if expected_size is not None:
        actual = os.path.getsize(tmp)
        if actual != expected_size:
            os.remove(tmp)
            raise RuntimeError(
                f"Download of {os.path.basename(dest)} looked incomplete or "
                f"corrupt ({actual} bytes, expected {expected_size}). "
                f"Will retry on next start."
            )
    os.replace(tmp, dest)


def _load_voice() -> None:
    global _voice
    from kokoro_onnx import Kokoro
    with _voice_lock:
        if _voice is None:
            _voice = Kokoro(_model_path(), _voices_path())


def _download_and_load() -> None:
    global _last_error
    try:
        os.makedirs(VOICES_DIR, exist_ok=True)
        if not os.path.exists(_voices_path()):
            logger.info("Downloading Kokoro voice bank (~27MB, one-time)...")
            _download_file(_RELEASE_URL + VOICES_FILENAME, _voices_path(),
                           expected_size=EXPECTED_VOICES_SIZE)
        if not os.path.exists(_model_path()):
            logger.info("Downloading Kokoro voice model (~310MB, one-time)...")
            _download_file(_RELEASE_URL + MODEL_FILENAME, _model_path(),
                           expected_size=EXPECTED_MODEL_SIZE)
        _load_voice()
        _last_error = None
        _ready_event.set()
        logger.info("Kokoro voice ready: %s", VOICE_NAME)
    except Exception as e:
        _last_error = str(e)
        logger.warning("Kokoro voice setup failed: %s", e)
        # Whatever's on disk didn't produce a working voice, whether it's a
        # fresh download or a stale cache from a previous bad run. Clear it
        # so the *next* attempt (next server start) downloads clean instead
        # of getting permanently stuck reloading the same broken file on
        # every future restart forever.
        _remove_cached_files()


def ensure_ready_async() -> None:
    """Kick off the one-time voice download/load in the background.

    Safe to call more than once; only the first call starts anything.
    """
    global _download_started
    with _download_lock:
        if _download_started:
            return
        _download_started = True

    if _files_present():
        try:
            _load_voice()
            _ready_event.set()
            return
        except Exception as e:
            global _last_error
            _last_error = str(e)
            logger.warning("Cached Kokoro voice failed to load, will "
                           "redownload: %s", e)
            _remove_cached_files()
            # fall through to the background download below

    threading.Thread(target=_download_and_load, daemon=True).start()


def is_ready() -> bool:
    return _ready_event.is_set() and _voice is not None


def last_error() -> str | None:
    return _last_error


def _to_wav_bytes(samples: np.ndarray, sample_rate: int) -> bytes:
    """Pack Kokoro's float32 waveform into a 16-bit PCM WAV.

    Kokoro returns floats nominally in [-1, 1]; clipping before the integer
    conversion keeps a rare overshoot from wrapping around into a loud click.
    """
    pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm.tobytes())
    return buf.getvalue()


def synthesize_wav(text: str) -> bytes:
    """Render text to WAV bytes with the local Kokoro voice.

    Raises RuntimeError if the voice isn't downloaded/loaded yet.
    """
    if not is_ready():
        raise RuntimeError(last_error() or "Voice model not ready yet.")
    text = (text or "").strip()
    if not text:
        raise ValueError("Empty text.")
    text = _spell_out_acronyms(text)

    with _voice_lock:
        samples, sample_rate = _voice.create(
            text, voice=VOICE_NAME, speed=SPEAKING_RATE, lang=LANG)
    return _to_wav_bytes(samples, sample_rate)
