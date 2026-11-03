"""Bounded, read-only reuse of completed preview reaction envelopes.

No decoding, FFT or analysis is initiated by readers. Fingerprints prevent
reusing results after a track file has changed at the same path.
"""
from collections import OrderedDict
from pathlib import Path


class AudioReactionCache:
    def __init__(self):
        self.entries = OrderedDict()

    @staticmethod
    def key(track):
        if not track.file_path:
            return None
        try:
            path = Path(track.file_path).resolve()
            stat = path.stat()
            return track.id, str(path), stat.st_size, stat.st_mtime_ns
        except OSError:
            return None

    def store(self, track, fps, analysis):
        key = self.key(track)
        if key is None or not isinstance(analysis, dict):
            return
        self.entries.pop(key, None)
        # Envelopes are already completed and never modified by either UI.
        self.entries[key] = (fps, analysis.get("bass"), dict(analysis.get("music_envelopes", {})))
        while len(self.entries) > 2:
            self.entries.popitem(last=False)

    def get(self, track):
        return self.entries.get(self.key(track))


REACTION_CACHE = AudioReactionCache()
