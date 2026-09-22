"""PCM cache and opt-in media serving, independent of the server/plugin lifecycle."""

import concurrent.futures
import subprocess

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
        from pathlib import Path
        Path(args[-1]).write_bytes(b"RIFF" + b"x" * 100)

    monkeypatch.setattr(browser_audio, "_ffmpeg_cmd", lambda: "ffmpeg")
    monkeypatch.setattr(browser_audio.subprocess, "run", decode)
    return source, tmp_path / "cache", calls


def test_cache_is_reused_without_changing_original(decoder):
    source, cache, calls = decoder
    result = browser_audio.browser_pcm_copy(source, cache)
    assert result.suffix == ".wav"
    assert result.read_bytes().startswith(b"RIFF")
    assert browser_audio.browser_pcm_copy(source, cache) == result
    assert len(calls) == 1
    assert source.read_bytes() == b"original compressed recording"
    args, options = calls[0]
    assert args[args.index("-c:a") + 1] == "pcm_s16le"
    assert "-ss" not in args  # full decode: no codec seek/padding ambiguity
    assert options["timeout"] == 120


def test_changed_source_gets_new_cache_entry(decoder):
    source, cache, calls = decoder
    first = browser_audio.browser_pcm_copy(source, cache)
    source.write_bytes(b"replacement recording of a different length")
    assert browser_audio.browser_pcm_copy(source, cache) != first
    assert len(calls) == 2


def test_concurrent_requests_decode_once(decoder):
    source, cache, calls = decoder
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: browser_audio.browser_pcm_copy(source, cache), range(4)))
    assert len(set(results)) == 1
    assert len(calls) == 1


def test_failed_conversion_leaves_no_partial_cache(decoder, monkeypatch):
    source, cache, _ = decoder

    def fail(*args, **kwargs):
        raise subprocess.TimeoutExpired("ffmpeg", 120)

    monkeypatch.setattr(browser_audio.subprocess, "run", fail)
    with pytest.raises(subprocess.TimeoutExpired):
        browser_audio.browser_pcm_copy(source, cache)
    assert list((cache / "browser-pcm").iterdir()) == []


def test_other_file_types_do_not_need_ffmpeg(tmp_path, monkeypatch):
    monkeypatch.setattr(browser_audio, "_ffmpeg_cmd", lambda: None)
    source = tmp_path / "original.wav"
    assert browser_audio.browser_pcm_copy(source, tmp_path) == source


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


def test_resolution_error_never_reaches_decoder(client, monkeypatch, decoder):
    monkeypatch.setattr(media, "_resolve_sloppak_local_file", lambda *args: ("forbidden", 403))
    assert client.get("/api/sloppak/song.feedpak/file/stems/full.ogg?playback=pcm").status_code == 403
    assert decoder[2] == []


def test_conversion_failure_is_reported_without_serving_bad_audio(client, monkeypatch):
    monkeypatch.setattr(browser_audio, "_ffmpeg_cmd", lambda: None)
    response = client.get("/audio/song.ogg?playback=pcm")
    assert response.status_code == 503
    assert response.json() == {"error": "Could not prepare seek-stable browser audio"}
