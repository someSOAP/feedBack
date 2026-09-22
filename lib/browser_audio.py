"""Seek-stable browser playback copies; never rewrite the source recording."""

import hashlib
import logging
import subprocess
import tempfile
import threading
from pathlib import Path

from audio import _ffmpeg_cmd

_decode_lock = threading.Lock()
_OGG_EXTENSIONS = {".ogg", ".oga", ".opus"}
log = logging.getLogger("feedBack.lib.browser_audio")


def _is_vorbis(source: Path) -> bool:
    # Vorbis requires its 30-byte identification packet alone on the first
    # Ogg page. Inspect only that header; unknown layouts take the WAV path.
    with source.open("rb") as f:
        header = f.read(27)
        if (len(header) != 27 or header[:5] != b"OggS\x00"
                or header[5] != 2 or header[26] != 1):
            return False
        return f.read(8) == b"\x1e\x01vorbis"


def _convert(source: Path, target: Path, ffmpeg: str, webm: bool):
    with tempfile.NamedTemporaryFile(dir=target.parent, suffix=target.suffix, delete=False) as f:
        temporary = Path(f.name)
    try:
        subprocess.run(
            [ffmpeg, "-nostdin", "-v", "error", "-y", "-i", str(source),
             "-map", "0:a:0", "-vn", "-c:a", "copy" if webm else "pcm_s16le",
             "-f", "webm" if webm else "wav", str(temporary)],
            check=True, capture_output=True, timeout=120,
        )
        if temporary.stat().st_size <= 44:
            raise RuntimeError("Audio conversion produced an empty file")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def browser_playback_copy(source: Path, cache_dir: Path, *, prefer_webm: bool = False) -> Path:
    """Avoid Chromium's Ogg seek drift without changing the source timeline.

    Chromium's Ogg playback can land on different PCM positions after seeking
    than it does during uninterrupted playback. A/V calibration cannot correct
    a seek-dependent offset. Vorbis-in-WebM retains the compressed packets but
    avoids the affected Ogg seek path. Other codecs, unsupported browsers and
    failed remuxes use WAV. Both retain HTMLAudioElement's speed/pitch controls.
    """
    if source.suffix.lower() not in _OGG_EXTENSIONS:
        return source
    stat = source.stat()
    version = "webm-v1" if prefer_webm else "pcm-v1"
    key = hashlib.sha256(
        f"{version}:{source.resolve()}:{stat.st_size}:{stat.st_mtime_ns}".encode()
    ).hexdigest()
    cache = Path(cache_dir) / ("browser-webm" if prefer_webm else "browser-pcm")
    target = cache / f"{key}.wav"
    remux = cache / f"{key}.webm"
    # Serialize conversions, and never expose partially written files
    # to simultaneous playback/range requests. Check the cache again in-lock.
    with _decode_lock:
        if prefer_webm and remux.is_file() and remux.stat().st_size > 44:
            return remux
        if target.is_file() and target.stat().st_size > 44:
            return target
        ffmpeg = _ffmpeg_cmd()
        if not ffmpeg:
            raise RuntimeError("FFmpeg is required for seek-stable Ogg playback")
        cache.mkdir(parents=True, exist_ok=True)
        if prefer_webm and _is_vorbis(source):
            try:
                _convert(source, remux, ffmpeg, webm=True)
                return remux
            except (OSError, RuntimeError, subprocess.SubprocessError):
                log.warning("WebM remux failed; using PCM browser audio", exc_info=True)
        # Cache fallback results too, so range requests don't retry a failed
        # remux. Keep the old PCM cache/key for clients that still request it.
        _convert(source, target, ffmpeg, webm=False)
    return target
