// Browser-only playback copy. Keep song metadata, native JUCE paths, downloads,
// and plugin stem URLs pointing at the original recording.
export function browserAudioUrl(url) {
    if (typeof url !== 'string') return url;
    const [pathAndQuery, fragment] = url.split('#', 2);
    const [path, query = ''] = pathAndQuery.split('?', 2);
    if (!/^\/(?:audio\/|api\/sloppak\/).+\.(?:ogg|oga|opus)$/i.test(path)) return url;
    const params = new URLSearchParams(query);
    params.set('playback', 'pcm');
    return path + '?' + params.toString() + (fragment === undefined ? '' : '#' + fragment);
}
