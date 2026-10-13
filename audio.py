"""Voice messages and audio uploads as prompts, transcribed on this machine.

Uses faster-whisper on the CPU: no API key, nothing leaves the box, a few seconds for a
typical voice note. The model loads on first use and is dropped again after
AUDIO_IDLE_UNLOAD seconds without audio so it doesn't sit in RAM. Idea and Discord flow
from recursiveforte/chertu (chert/discord/audio.py, MIT); the transcription backend differs.

Settings: AUDIO_TRANSCRIBE=0 turns it off; AUDIO_MODEL (default "small": tiny/base/small/
medium/large-v3/turbo, or a local CTranslate2 model dir); AUDIO_LANGUAGE (blank = detect);
AUDIO_THREADS (CPU threads, default 4); AUDIO_MAX_SECS (longer clips are refused).
"""
import os
import tempfile
import threading
import time
from pathlib import Path

ENABLED = os.environ.get("AUDIO_TRANSCRIBE", "1") == "1"
MODEL = os.environ.get("AUDIO_MODEL", "small")
LANGUAGE = os.environ.get("AUDIO_LANGUAGE", "").strip() or None
THREADS = int(os.environ.get("AUDIO_THREADS", "4"))
MAX_SECS = int(os.environ.get("AUDIO_MAX_SECS", "900"))
IDLE_UNLOAD = int(os.environ.get("AUDIO_IDLE_UNLOAD", "1800"))
EXTS = (".ogg", ".oga", ".opus", ".mp3", ".m4a", ".wav", ".webm", ".flac", ".aac", ".mp4", ".mov")
# words Whisper otherwise hears as "shirt", "Olin's"...; the bridge adds session/project names
BASE_VOCAB = ("chert", "Claude", "Codex", "Astra", "Feldspar", "subagent", "tmux", "yolo")

_model = None
_last_used = 0.0
_lock = threading.Lock()


def available():
    if not ENABLED:
        return False
    try:
        import faster_whisper  # noqa: F401
        return True
    except ImportError:
        return False


def is_audio(att):
    """A Discord attachment that is a voice message or an audio file."""
    try:
        if att.is_voice_message():
            return True
    except AttributeError:
        pass
    ctype = (getattr(att, "content_type", "") or "").lower()
    name = (getattr(att, "filename", "") or "").lower()
    return ctype.startswith("audio/") or name.endswith(EXTS[:-2]) or (
        ctype.startswith("video/") and name.endswith((".mp4", ".mov", ".webm")) and att.size < 30_000_000)


def _get_model():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        _model = WhisperModel(MODEL, device="cpu", compute_type="int8", cpu_threads=THREADS)
    return _model


def transcribe_file(path, vocab=()):
    """(text, seconds_of_audio, language). Blocking: call through asyncio.to_thread. One clip at
    a time, so two voice notes can't double the CPU and RAM load."""
    global _last_used
    with _lock:
        model = _get_model()
        words = ", ".join(dict.fromkeys(w for w in (*BASE_VOCAB, *vocab) if w))[:600]
        segments, info = model.transcribe(str(path), language=LANGUAGE, vad_filter=True, beam_size=5,
                                          hotwords=words or None)
        if info.duration > MAX_SECS:
            raise ValueError(f"audio is {info.duration / 60:.0f} min long (limit {MAX_SECS // 60} min)")
        text = " ".join(s.text.strip() for s in segments).strip()
        _last_used = time.time()
        return text, info.duration, info.language


async def transcribe_attachment(att, vocab=()):
    """Download one Discord attachment to a temp file, transcribe it, delete the file."""
    import asyncio
    data = await att.read()
    suffix = Path(att.filename or "audio.ogg").suffix or ".ogg"
    fd, tmp = tempfile.mkstemp(prefix="chert-audio-", suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return await asyncio.to_thread(transcribe_file, tmp, tuple(vocab))
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def maybe_unload():
    """Free the model's RAM after a quiet spell (called from the bridge's periodic tick)."""
    global _model
    if _model is not None and time.time() - _last_used > IDLE_UNLOAD and _lock.acquire(blocking=False):
        try:
            _model = None
        finally:
            _lock.release()
