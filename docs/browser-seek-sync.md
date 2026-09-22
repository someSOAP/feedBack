# Browser seek synchronization

## Reproduction and scope

Reported on macOS, Chrome 153.0.8010.50, built-in speakers, Docker-hosted
feedBack, Rocksmith Highway visualization. Clicking a section or pressing
Left/Right triggers the problem reliably; pause/resume alone is not established
as its cause. The user reports roughly 100 ms, with -100 ms A/V compensation
making it appear aligned.

The active transport is HTMLAudioElement, not JUCE. Both section navigation and
arrow keys reach `_audioSeek`, which sets the element's `currentTime`. The
visualization's clock follows the host sample; measurements showed normal
sub-frame sampling lag, not a persistent extra 100 ms in the renderer.

## Audio-level evidence

The diagnostic `scripts/check-browser-seek.cjs` compares emitted PCM against a
full decode of the same recording. It first plays continuously (without seeking)
to establish a baseline, then seeks forward/backward and pauses/resumes. It
correlates actual sample blocks, not merely two JavaScript clock readings.
Audio is observed before the speaker output; this measures **seek-dependent
changes**, not acoustic/device latency. An absolute probe offset is not an A/V
calibration value.

Initial probes of the original Ogg recordings showed a repeatable ~21.3 ms
change after seeking in Chromium 151, with additional inconsistent seek results
in Chrome 153. The committed probe on the original The Bad Touch Ogg measured
8.285 ms continuously, -50.025 ms after seeking to 30 seconds, and about
-13.1 ms after seeking to 90/100 seconds. At 20 seconds no reliable match was
found in the search window, so that run correctly reported itself inconclusive
rather than declaring a precise offset. The user's full 100 ms acoustic discrepancy has not been measured
directly. Therefore it should not be presented as a proven universal constant.

The patched server's decoded WAV, tested in Chrome 153 with The Bad Touch,
produced the following PCM-relative offsets (ms): continuous 8.220; seeks to
30/90/20/100 seconds: 8.308 / 8.167 / 8.282 / 8.068; pause/resume 8.166.
The spread was **0.24 ms**. This removes the reproduced compressed-seek error
without inserting a hard-coded -100 ms offset or changing chart timestamps.

## Fix

Core HTML5 playback adds `?playback=pcm` to local Ogg audio URLs. The existing
media endpoints resolve the source using their original containment checks,
decode it once from the beginning with FFmpeg, and return a range-seekable WAV.
Seeking now addresses PCM samples rather than restarting an Ogg decoder at
compressed packet boundaries. Playback speed and pitch preservation still use
the existing HTMLAudioElement. Native JUCE gets the original URL/path.

The cache is `audio_cache_dir/browser-pcm/`, keyed by source path, size, and
nanosecond modification time. Publication is atomic; concurrent requests reuse
one conversion. Errors return 503 instead of silently claiming to provide a
seek-stable copy. FFmpeg is required. Stereo 48 kHz, 16-bit PCM costs about
11.5 MB per minute, and the first request incurs conversion time. Source packs,
plugin repositories, settings, and chart offsets are not modified. Cache files
can be discarded while playback is stopped and will be regenerated on demand.

## Verification

Run with a local song longer than 105 seconds and audible content around the
test positions (no copyrighted recording is bundled with the diagnostic):

```sh
node scripts/check-browser-seek.cjs 'http://localhost:8000/api/sloppak/SONG.feedpak/file/stems/full.ogg?playback=pcm'
```

Set `SEEK_BROWSER_EXECUTABLE` to a Chrome binary to test that exact version in a
disposable profile. Remove the query string to compare original Ogg playback.
The probe fails for >10 ms spread or inconclusive/silent samples.

Automated verification: the 9 new backend tests cover caching, invalidation,
concurrent requests, cleanup after decoder failure, opt-in responses, range
requests, and source-resolution failure. The JS suite passes. `transport.js`
was not changed by this fix. A full application run confirmed the PCM URL was
selected, chart/media clock agreement within 0.3 ms at the sampled positions,
arrow-key transport seeks, paused seeks, and 0.7x playback with pitch
preservation enabled.
The combined backend run (new cache tests plus existing audio-decoder and
local-path safety tests) passed all 55 tests.

For the user-facing check: fully reload the page, reset the temporary -100 ms
workaround to the usual A/V setting (start with 0), reopen the song, then compare
continuous playback, section clicks, Left/Right, repeated seeks, paused seeks,
and slow playback. The browser audio request should have `playback=pcm` and an
`audio/wav` response. A fixed device/calibration offset, if present independently
of seeking, is a separate issue; do not conflate it with decoder seek drift.
