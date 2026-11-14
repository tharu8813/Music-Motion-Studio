"""Project-wide audio effect settings shared by the dialog and mixer."""

from dataclasses import dataclass
from math import isfinite


@dataclass(frozen=True, slots=True)
class EffectParameter:
    key: str
    label: tuple[str, str]
    low: float
    high: float
    default: float
    step: float
    unit: str


@dataclass(frozen=True, slots=True)
class AudioEffect:
    key: str
    label: tuple[str, str]
    description: tuple[str, str]
    parameters: tuple[EffectParameter, ...]


PERIOD = EffectParameter("period", ("이동 주기", "Cycle time"), 1, 30, 8, 0.5, "s")
STRENGTH = EffectParameter("strength", ("이동 강도", "Movement depth"), 0, 100, 70, 5, "%")

# Processing order also determines the order of the effect controls.
AUDIO_EFFECTS = (
    AudioEffect("stereo_width", ("스테레오 확장", "Stereo widening"), (
        "이미 좌우로 나뉘어 들리는 소리를 더 넓게 펼칩니다.",
        "Spreads existing left and right sounds farther apart.",
    ), (
        EffectParameter("width", ("스테레오 폭", "Stereo width"), 100, 200, 140, 5, "%"),
    )),
    AudioEffect("chorus", ("코러스", "Chorus"), (
        "타이밍과 음높이가 조금씩 다른 소리를 겹쳐 여러 사람이 함께 연주하는 듯 풍성하게 들립니다.",
        "Layers slightly delayed, pitch-shifted copies so the music sounds fuller, like several performers playing together.",
    ), (
        EffectParameter("mix", ("효과 강도", "Effect level"), 0, 100, 30, 5, "%"),
        EffectParameter("rate", ("변조 속도", "Modulation rate"), 0.2, 3, 0.6, 0.1, "Hz"),
        EffectParameter("depth", ("변조 깊이", "Modulation depth"), 0.5, 8, 2, 0.5, "ms"),
    )),
    AudioEffect("auto_pan", ("오토팬", "Auto pan"), (
        "소리가 왼쪽과 오른쪽 사이를 부드럽게 왕복합니다.",
        "Moves the sound smoothly between left and right.",
    ), (PERIOD, STRENGTH)),
    AudioEffect("eight_d", ("8D 오디오", "8D audio"), (
        "좌우로 움직이는 음악에 잔향을 더해 공간 속을 떠다니는 듯한 느낌을 줍니다.",
        "Adds room reverb to the side-to-side movement so the music feels like it is drifting through a space.",
    ), (
        PERIOD, STRENGTH, EffectParameter("space", ("잔향 강도", "Room level"), 0, 100, 20, 5, "%"),
    )),
    AudioEffect("binaural", ("바이노럴 회전 (헤드폰)", "Binaural rotation (headphones)"), (
        "헤드폰에서 소리가 앞뒤와 좌우를 지나 머리 주위를 도는 듯한 느낌을 줍니다.",
        "Makes the sound seem to circle your head, passing in front, to the sides, and behind you when heard on headphones.",
    ), (
        EffectParameter("period", ("회전 주기", "Rotation time"), 1, 30, 8, 0.5, "s"),
        EffectParameter("mix", ("효과 강도", "Effect level"), 0, 100, 80, 5, "%"),
    )),
    AudioEffect("ping_pong", ("핑퐁 딜레이", "Ping-pong delay"), (
        "원음 뒤에 메아리가 왼쪽과 오른쪽에서 번갈아 들리며 점점 작아집니다.",
        "Repeats the sound as echoes that alternate between left and right and gradually fade.",
    ), (
        EffectParameter("delay", ("반복 간격", "Repeat interval"), 50, 1000, 250, 10, "ms"),
        EffectParameter("feedback", ("반복 감쇠율", "Level retained per repeat"), 0, 80, 45, 5, "%"),
        EffectParameter("mix", ("첫 메아리 강도", "First echo level"), 0, 100, 25, 5, "%"),
    )),
    AudioEffect("reverb", ("리버브 (잔향)", "Reverb"), (
        "방이나 공연장에서처럼 소리가 공간에 퍼지고 서서히 사라지는 울림을 더합니다.",
        "Adds a lingering sound that fades as it would in a room or concert hall.",
    ), (
        EffectParameter("decay", ("잔향 길이", "Reverb length"), 0.2, 4, 1.2, 0.1, "s"),
        EffectParameter("mix", ("잔향 강도", "Reverb level"), 0, 100, 25, 5, "%"),
    )),
)

AudioEffectSettings = dict[str, dict[str, float | bool]]


def normalize_audio_effects(value: object) -> AudioEffectSettings:
    """Keep supported, bounded values from a project payload."""
    effects = {}
    if not isinstance(value, dict):
        return effects
    for effect in AUDIO_EFFECTS:
        saved = value.get(effect.key)
        if not isinstance(saved, dict):
            continue
        values = {"enabled": saved.get("enabled") is True}
        for parameter in effect.parameters:
            number = saved.get(parameter.key, parameter.default)
            if type(number) not in (int, float) or (isinstance(number, float) and not isfinite(number)):
                number = parameter.default
            values[parameter.key] = float(max(parameter.low, min(parameter.high, number)))
        effects[effect.key] = values
    return effects


def has_audio_effects(effects: AudioEffectSettings | None) -> bool:
    return bool(effects and any(values["enabled"] for values in effects.values()))
