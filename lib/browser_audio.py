"""Seek-stable browser playback copies; never rewrite the source recording."""

import hashlib
import subprocess
import tempfile
import threading
from pathlib import Path

from audio import _ffmpeg_cmd

_decode_lock = threading.Lock()
_OGG_EXTENSIONS = {".ogg", ".oga", ".opus"}


def browser_pcm_copy(source: Path, cache_dir: Path) -> Path:
    """Decode Ogg once, so browser seeks address PCM samples, not codec preroll.

    Chromium's Ogg playback can land on different PCM positions after seeking
    than it does during uninterrupted playback. A/V calibration cannot correct
    a seek-dependent offset. WAV keeps HTMLAudioElement (including pitch-
    preserving slow playback) but removes compressed-packet seeking entirely.
    """
    if source.suffix.lower() not in _OGG_EXTENSIONS:
        return source
    stat = source.stat()
    key = hashlib.sha256(
        f"pcm-v1:{source.resolve()}:{stat.st_size}:{stat.st_mtime_ns}".encode()
    ).hexdigest()
    cache = Path(cache_dir) / "browser-pcm"
    target = cache / f"{key}.wav"
    # Serialize expensive conversions, and never expose partially written WAVs
    # to simultaneous playback/range requests. Check the cache again in-lock.
    with _decode_lock:
        if target.is_file() and target.stat().st_size > 44:
            return target
        ffmpeg = _ffmpeg_cmd()
        if not ffmpeg:
            raise RuntimeError("FFmpeg is required for seek-stable Ogg playback")
        cache.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=cache, suffix=".wav", delete=False) as f:
            temporary = Path(f.name)
        try:
            subprocess.run(
                [ffmpeg, "-nostdin", "-v", "error", "-y", "-i", str(source),
                 "-map", "0:a:0", "-vn", "-c:a", "pcm_s16le", "-f", "wav", str(temporary)],
                check=True, capture_output=True, timeout=120,
            )
            if temporary.stat().st_size <= 44:
                raise RuntimeError("Audio decoder produced an empty WAV")
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
    return target
