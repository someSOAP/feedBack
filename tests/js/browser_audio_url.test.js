const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '../../static/js/browser-audio-url.js'), 'utf8');
const context = { URLSearchParams };
vm.createContext(context);
vm.runInContext(source.replace('export function', 'function'), context);
const url = context.browserAudioUrl;

test('only core Ogg browser URLs request a PCM copy', () => {
    assert.equal(url('/audio/song.ogg'), '/audio/song.ogg?playback=pcm');
    assert.equal(url('/api/sloppak/My%20Song.feedpak/file/stems/full.ogg'),
        '/api/sloppak/My%20Song.feedpak/file/stems/full.ogg?playback=pcm');
    for (const unchanged of [null, '', '/audio/song.wav', '/audio/song.mp3',
        'blob:recording', 'https://example.com/song.ogg', '/api/plugins/stems/song.ogg']) {
        assert.equal(url(unchanged), unchanged);
    }
});

test('preserves query and fragment and does not accumulate flags', () => {
    const input = '/audio/song.ogg?v=3#part';
    const expected = '/audio/song.ogg?v=3&playback=pcm#part';
    assert.equal(url(input), expected);
    assert.equal(url(expected), expected);
});
