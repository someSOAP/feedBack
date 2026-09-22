"""Seek-stable caches and opt-in serving, independent of the server lifecycle."""

import concurrent.futures
import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import browser_audio
from routers import media


@pytest.fixture
def decoder(tmp_path, monkeypatch):
    source = tmp_path / "song.ogg"
    source.write_bytes(b"original compressed recording")
    calls = []

    def decode(args, **kwargs):
        calls.append((args, kwargs))
        # The cache publishes only a finished file, not this temporary output.
        prefix = b"\x1aE\xdf\xa3" if args[args.index("-f") + 1] == "webm" else b"RIFF"
        Path(args[-1]).write_bytes(prefix + b"x" * 100)

    monkeypatch.setattr(browser_audio, "_ffmpeg_cmd", lambda: "ffmpeg")
    monkeypatch.setattr(browser_audio.subprocess, "run", decode)
    return source, tmp_path / "cache", calls


def test_cache_is_reused_without_changing_original(decoder):
    source, cache, calls = decoder
    result = browser_audio.browser_playback_copy(source, cache)
    assert result.suffix == ".wav"
    assert result.read_bytes().startswith(b"RIFF")
    assert browser_audio.browser_playback_copy(source, cache) == result
    assert len(calls) == 1
    assert source.read_bytes() == b"original compressed recording"
    args, options = calls[0]
    assert args[args.index("-c:a") + 1] == "pcm_s16le"
    assert "-ss" not in args  # full decode: no codec seek/padding ambiguity
    assert options["timeout"] == 120


def test_changed_source_gets_new_cache_entry(decoder):
    source, cache, calls = decoder
    first = browser_audio.browser_playback_copy(source, cache)
    source.write_bytes(b"replacement recording of a different length")
    assert browser_audio.browser_playback_copy(source, cache) != first
    assert len(calls) == 2


def test_concurrent_requests_decode_once(decoder):
    source, cache, calls = decoder
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: browser_audio.browser_playback_copy(source, cache), range(4)))
    assert len(set(results)) == 1
    assert len(calls) == 1


def test_failed_conversion_leaves_no_partial_cache(decoder, monkeypatch):
    source, cache, _ = decoder

    def fail(*args, **kwargs):
        raise subprocess.TimeoutExpired("ffmpeg", 120)

    monkeypatch.setattr(browser_audio.subprocess, "run", fail)
    with pytest.raises(subprocess.TimeoutExpired):
        browser_audio.browser_playback_copy(source, cache)
    assert list((cache / "browser-pcm").iterdir()) == []


def test_other_file_types_do_not_need_ffmpeg(tmp_path, monkeypatch):
    monkeypatch.setattr(browser_audio, "_ffmpeg_cmd", lambda: None)
    source = tmp_path / "original.wav"
    assert browser_audio.browser_playback_copy(source, tmp_path) == source


@pytest.fixture
def vorbis(decoder):
    source, cache, calls = decoder
    source.write_bytes(b"OggS\x00\x02" + b"\x00" * 20 + b"\x01\x1e\x01vorbis" + b"\x00" * 23)
    return source, cache, calls


def test_webm_copies_packets_and_reuses_cache(vorbis):
    source, cache, calls = vorbis
    original = source.read_bytes()
    result = browser_audio.browser_playback_copy(source, cache, prefer_webm=True)
    assert result.suffix == ".webm"
    assert source.read_bytes() == original
    assert browser_audio.browser_playback_copy(source, cache, prefer_webm=True) == result
    assert len(calls) == 1
    args, _ = calls[0]
    assert args[args.index("-c:a") + 1] == "copy"
    assert args[args.index("-map") + 1] == "0:a:0"
    assert "-ss" not in args
    # A browser without WebM support still gets its separate WAV cache.
    assert browser_audio.browser_playback_copy(source, cache).suffix == ".wav"


def test_webm_cache_invalidates_on_source_change(vorbis):
    source, cache, calls = vorbis
    first = browser_audio.browser_playback_copy(source, cache, prefer_webm=True)
    source.write_bytes(source.read_bytes() + b"changed")
    assert browser_audio.browser_playback_copy(source, cache, prefer_webm=True) != first
    assert len(calls) == 2


def test_concurrent_webm_requests_remux_once(vorbis):
    source, cache, calls = vorbis
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(
            lambda _: browser_audio.browser_playback_copy(source, cache, prefer_webm=True), range(4)))
    assert len(set(results)) == 1
    assert len(calls) == 1


@pytest.mark.parametrize("content", [b"", b"OggS", b"OggS\x00\x02" + b"\x00" * 20 + b"\x01\x13OpusHead",
                                     b"unrecognized recording"])
def test_unknown_or_opus_headers_use_wav(decoder, content):
    source, cache, calls = decoder
    source.write_bytes(content)
    assert browser_audio.browser_playback_copy(source, cache, prefer_webm=True).suffix == ".wav"
    assert len(calls) == 1
    assert calls[0][0][calls[0][0].index("-c:a") + 1] == "pcm_s16le"


@pytest.mark.parametrize("failure", ["error", "timeout", "empty"])
def test_remux_failure_caches_wav_fallback(vorbis, monkeypatch, failure):
    source, cache, calls = vorbis
    convert = browser_audio.subprocess.run
    attempts = []

    def fail_remux(args, **kwargs):
        attempts.append(args)
        if args[args.index("-f") + 1] == "webm":
            Path(args[-1]).write_bytes(b"partial")
            if failure == "timeout":
                raise subprocess.TimeoutExpired("ffmpeg", 120)
            if failure == "error":
                raise subprocess.CalledProcessError(1, "ffmpeg")
            return  # successful exit but empty/truncated output
        return convert(args, **kwargs)

    monkeypatch.setattr(browser_audio.subprocess, "run", fail_remux)
    result = browser_audio.browser_playback_copy(source, cache, prefer_webm=True)
    assert result.suffix == ".wav"
    assert browser_audio.browser_playback_copy(source, cache, prefer_webm=True) == result
    assert len(attempts) == 2
    assert list((cache / "browser-webm").iterdir()) == [result]


def test_both_conversions_failing_leaves_no_partial_files(vorbis, monkeypatch):
    source, cache, _ = vorbis

    def fail(args, **kwargs):
        Path(args[-1]).write_bytes(b"partial")
        raise subprocess.CalledProcessError(1, "ffmpeg")

    monkeypatch.setattr(browser_audio.subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        browser_audio.browser_playback_copy(source, cache, prefer_webm=True)
    assert list((cache / "browser-webm").iterdir()) == []


@pytest.fixture
def client(decoder, monkeypatch, tmp_path):
    source, cache, _ = decoder
    monkeypatch.setattr(media, "_resolve_sloppak_local_file", lambda *args: source)
    monkeypatch.setattr(media.appstate, "audio_cache_dir", cache)
    monkeypatch.setattr(media.appstate, "static_dir", tmp_path)
    app = FastAPI()
    app.include_router(media.router)
    with TestClient(app) as client:
        yield client


@pytest.mark.parametrize("url", ["/audio/song.ogg", "/api/sloppak/song.feedpak/file/stems/full.ogg"])
def test_pcm_is_opt_in_and_supports_range_requests(client, decoder, url):
    original = client.get(url)
    assert original.content == b"original compressed recording"
    result = client.get(url + "?playback=pcm")
    assert result.headers["content-type"] in ("audio/wav", "audio/x-wav", "audio/vnd.wave")
    assert result.content.startswith(b"RIFF")
    ranged = client.get(url + "?playback=pcm", headers={"Range": "bytes=4-11"})
    assert ranged.status_code == 206
    assert ranged.content == result.content[4:12]
    assert len(decoder[2]) == 1


@pytest.mark.parametrize("playback", ["pcm", "webm"])
def test_resolution_error_never_reaches_decoder(client, monkeypatch, decoder, playback):
    monkeypatch.setattr(media, "_resolve_sloppak_local_file", lambda *args: ("forbidden", 403))
    assert client.get(f"/api/sloppak/song.feedpak/file/stems/full.ogg?playback={playback}").status_code == 403
    assert decoder[2] == []


@pytest.mark.parametrize("playback", ["pcm", "webm"])
def test_conversion_failure_is_reported_without_serving_bad_audio(client, monkeypatch, playback):
    monkeypatch.setattr(browser_audio, "_ffmpeg_cmd", lambda: None)
    response = client.get(f"/audio/song.ogg?playback={playback}")
    assert response.status_code == 503
    assert response.json() == {"error": "Could not prepare seek-stable browser audio"}


@pytest.mark.parametrize("url", ["/audio/song.ogg", "/api/sloppak/song.feedpak/file/stems/full.ogg"])
def test_webm_is_opt_in_and_range_seekable(client, vorbis, url):
    source, _, calls = vorbis
    assert client.get(url).content == source.read_bytes()
    result = client.get(url + "?playback=webm")
    assert result.headers["content-type"] == "audio/webm"
    assert result.content.startswith(b"\x1aE\xdf\xa3")
    ranged = client.get(url + "?playback=webm", headers={"Range": "bytes=4-11"})
    assert ranged.status_code == 206
    assert ranged.content == result.content[4:12]
    assert len(calls) == 1


def test_webm_request_can_return_wav(client):
    result = client.get("/audio/song.ogg?playback=webm")
    assert result.headers["content-type"] in ("audio/wav", "audio/x-wav", "audio/vnd.wave")
    assert result.content.startswith(b"RIFF")


@pytest.mark.parametrize("playback", ["pcm", "webm"])
def test_audio_traversal_rejected_before_conversion(client, decoder, playback):
    result = client.get(f"/audio/%2e%2e/song.ogg?playback={playback}")
    assert result.status_code == 404
    assert decoder[2] == []
