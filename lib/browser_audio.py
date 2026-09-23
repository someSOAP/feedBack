"""Seek-stable browser playback copies; never rewrite the source recording."""

import hashlib
import logging
import re
import subprocess
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

from audio import _ffmpeg_cmd

_locks_guard = threading.Lock()
_key_locks: dict[str, tuple[threading.Lock, int]] = {}
_eviction_lock = threading.Lock()
_OGG_EXTENSIONS = {".ogg", ".oga", ".opus"}
_MAX_BROWSER_COPIES = 100
log = logging.getLogger("feedBack.lib.browser_audio")


@contextmanager
def _conversion_lock(key: str):
    """Share a lock with callers converting the same source version only."""
    with _locks_guard:
        lock, users = _key_locks.get(key, (threading.Lock(), 0))
        _key_locks[key] = (lock, users + 1)
    try:
        with lock:
            yield
    finally:
        with _locks_guard:
            _, users = _key_locks[key]
            if users == 1:
                del _key_locks[key]
            else:
                _key_locks[key] = (lock, users - 1)


def _evict_browser_copies(cache_dir: Path, keep: Path) -> None:
    """Bound cached browser copies independently of the original audio cache."""
    with _eviction_lock:
        try:
            files = [path for directory in ("browser-pcm", "browser-webm")
                     for path in (Path(cache_dir) / directory).glob("*")
                     if path.is_file() and path.suffix in (".wav", ".webm")
                     and re.fullmatch(r"[0-9a-f]{64}", path.stem)]
            if len(files) <= _MAX_BROWSER_COPIES:
                return
            files.sort(key=lambda path: path.stat().st_atime_ns)
            for path in [path for path in files if path != keep][:len(files) - _MAX_BROWSER_COPIES]:
                path.unlink(missing_ok=True)
        except OSError:
            log.debug("Browser audio cache eviction failed for %s", cache_dir, exc_info=True)


def _is_vorbis(source: Path) -> bool:
    """Return whether the first Ogg page contains a Vorbis ID packet."""
    # Vorbis requires its 30-byte identification packet alone on the first
    # Ogg page. Inspect only that header; unknown layouts take the WAV path.
    with source.open("rb") as f:
        header = f.read(27)
        if (len(header) != 27 or header[:5] != b"OggS\x00"
                or header[5] != 2 or header[26] != 1):
            return False
        return f.read(8) == b"\x1e\x01vorbis"


def _convert(source: Path, target: Path, ffmpeg: str, webm: bool):
    """Publish a complete converted file atomically, removing failed output."""
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
    def cached_copy():
        """Find a published copy, including a cached WAV remux fallback."""
        if prefer_webm and remux.is_file() and remux.stat().st_size > 44:
            return remux
        if target.is_file() and target.stat().st_size > 44:
            return target
        return None

    hit = cached_copy()
    if hit is not None:
        return hit
    # Check again after taking the key lock so concurrent requests convert once.
    with _conversion_lock(key):
        hit = cached_copy()
        if hit is not None:
            return hit
        ffmpeg = _ffmpeg_cmd()
        if not ffmpeg:
            raise RuntimeError("FFmpeg is required for seek-stable Ogg playback")
        cache.mkdir(parents=True, exist_ok=True)
        if prefer_webm and _is_vorbis(source):
            try:
                _convert(source, remux, ffmpeg, webm=True)
                _evict_browser_copies(cache_dir, remux)
                return remux
            except (OSError, RuntimeError, subprocess.SubprocessError):
                log.warning("WebM remux failed; using PCM browser audio", exc_info=True)
        # Cache fallback results too, so range requests don't retry a failed
        # remux. Keep the old PCM cache/key for clients that still request it.
        _convert(source, target, ffmpeg, webm=False)
        _evict_browser_copies(cache_dir, target)
    return target
