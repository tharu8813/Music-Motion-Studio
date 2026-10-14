"""Two preloaded Qt decks for transitions while the export-quality mix renders."""

from math import cos, pi, sin
from pathlib import Path

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

from app.timeline.models import TransitionType


class LiveMixPlayer(QObject):
    failed = Signal(str)

    def __init__(self, tracks, parent=None):
        super().__init__(parent)
        self.tracks = {track.id: track for track in tracks}
        self.players = [QMediaPlayer(self), QMediaPlayer(self)]
        self.outputs = [QAudioOutput(self), QAudioOutput(self)]
        self.clips = [None, None]
        self.pending = [False, False]
        self.plan = None
        self.seconds = 0.0
        self.playing = False
        self.volume = 1.0
        for index, (player, output) in enumerate(zip(self.players, self.outputs)):
            output.setVolume(0.0)
            player.setAudioOutput(output)
            if hasattr(player, "setPitchCompensation"):
                player.setPitchCompensation(True)
            player.mediaStatusChanged.connect(lambda _status, i=index: self._ready(i))
            player.seekableChanged.connect(lambda _value, i=index: self._ready(i))
            player.errorOccurred.connect(lambda _error, message, i=index: self._failed(i, message))

    def sync(self, plan, seconds, playing, volume, *, seek=False):
        self.plan, self.seconds, self.playing, self.volume = plan, seconds, playing, volume
        active = [clip for clip in plan.audio.clips if clip.timeline_start <= seconds < clip.timeline_end]
        wanted = list(active)
        if len(wanted) < 2:
            upcoming = next((clip for clip in plan.audio.clips
                             if seconds < clip.timeline_start <= seconds + 3.0), None)
            if upcoming is not None:
                wanted.append(upcoming)
        for index, clip in enumerate(self.clips):
            if clip is not None and clip not in wanted:
                self.outputs[index].setVolume(0.0)
                self.players[index].stop()
                self.clips[index] = None
                self.pending[index] = False
        for clip in wanted:
            if clip in self.clips:
                index = self.clips.index(clip)
            else:
                index = self.clips.index(None)
                self.clips[index] = clip
                self.pending[index] = True
                player = self.players[index]
                url = QUrl.fromLocalFile(str(Path(self.tracks[clip.track_id].file_path).resolve()))
                if player.source() != url:
                    player.setSource(url)
            if seek:
                self.pending[index] = True
            self._update(index)

    def _ready(self, index):
        if self.clips[index] is not None:
            self._update(index)

    def _update(self, index):
        clip = self.clips[index]
        player = self.players[index]
        output = self.outputs[index]
        if clip is None:
            return
        active = clip.timeline_start <= self.seconds < clip.timeline_end
        if player.mediaStatus() not in (
            QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia,
            QMediaPlayer.MediaStatus.EndOfMedia,
        ) or not player.isSeekable():
            output.setVolume(0.0)
            return
        source = clip.source_at(self.seconds)
        rate = next((rate for start, end, rate in clip.rate_segments() if start <= source < end),
                    clip.playback_rate)
        player.setPlaybackRate(rate)
        if self.pending[index] or (active and self.playing and
                                  abs(player.position() / 1000.0 - source) > 0.18):
            player.setPosition(round(source * 1000))
            self.pending[index] = False
        gain = 1.0
        for transition in self.plan.audio.transitions:
            if transition.timeline_start <= self.seconds < transition.timeline_start + transition.duration:
                progress = (self.seconds - transition.timeline_start) / transition.duration
                if clip.clip_id == transition.clip_a:
                    incoming_index = next((i for i, candidate in enumerate(self.clips)
                                           if candidate is not None and candidate.clip_id == transition.clip_b), None)
                    if incoming_index is None or self.players[incoming_index].mediaStatus() not in (
                        QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia,
                    ) or not self.players[incoming_index].isSeekable():
                        continue  # Keep the outgoing sound while the next file cannot play yet.
                    gain *= (1 - progress if transition.type == TransitionType.CROSSFADE
                             else cos(progress * pi / 2))
                elif clip.clip_id == transition.clip_b:
                    gain *= (progress if transition.type == TransitionType.CROSSFADE
                             else sin(progress * pi / 2))
        track = self.tracks[clip.track_id]
        # shortcut: raw decks omit EQ and styled DSP; use the rendered mix for exact export sound.
        output.setVolume(min(1.0, max(0.0, self.volume * clip.gain * 10 ** (track.volume_db / 20) * gain))
                         if active else 0.0)
        if active and self.playing:
            if player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
                player.play()
        else:
            player.pause()

    def pause(self):
        self.playing = False
        for player in self.players:
            player.pause()

    def stop(self):
        self.playing = False
        self.clips = [None, None]
        self.pending = [False, False]
        for player, output in zip(self.players, self.outputs):
            output.setVolume(0.0)
            player.stop()
            player.setSource(QUrl())

    def _failed(self, index, message):
        self.outputs[index].setVolume(0.0)
        self.players[index].stop()
        self.pending[index] = False
        self.failed.emit(message)
