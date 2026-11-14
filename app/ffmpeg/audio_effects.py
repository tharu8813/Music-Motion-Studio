"""Build the same stereo effects for preview and export, after the tracks are joined."""

from math import pi

from app.models.audio_effects import AudioEffectSettings, normalize_audio_effects
from app.renderer.loudness import TRUE_PEAK_DBTP


def _pan(period: float, strength: float) -> str:
    return f"apulsator=hz={1 / period:.8f}:amount={strength / 100:g}:offset_l=0:offset_r=0.5"


def _reverb(source: str, output: str, prefix: str, decay: float, level: float) -> str:
    # A diffuse stereo impulse with an exponential 60 dB decay and a short pre-delay.
    envelope = f"exp(-6.91*t/{decay:g})"
    return (
        f"anoisesrc=r=48000:d={decay:g}:seed=37[a{prefix}];"
        f"anoisesrc=r=48000:d={decay:g}:seed=83[b{prefix}];"
        f"[a{prefix}][b{prefix}]amerge,highpass=f=170,lowpass=f=6500,"
        f"aeval='val(0)*{envelope}|val(1)*{envelope}',adelay=17:all=1[ir{prefix}];"
        f"[{source}]asplit[dry{prefix}][wet{prefix}];"
        f"[wet{prefix}][ir{prefix}]afir=irnorm=2:minp=1024:maxp=8192[r{prefix}];"
        f"[dry{prefix}][r{prefix}]amix=duration=first:normalize=0:weights='1 {level:g}'[{output}]"
    )


def _ping_pong(source: str, output: str, delay: float, feedback: float, mix: float) -> str:
    # Limit the echo train to 12 taps. At maximum feedback the last tap is 22 dB down.
    gains = [mix * feedback ** index for index in range(12)]
    gains = [gain for index, gain in enumerate(gains) if index == 0 or gain >= 0.005]
    inputs = "".join(f"[echo{i}]" for i in range(len(gains)))
    taps = []
    for index, gain in enumerate(gains):
        pan = "FL=0.5*FL+0.5*FR|FR=0*FR" if index % 2 == 0 else "FL=0*FL|FR=0.5*FL+0.5*FR"
        taps.append(f"[echo{index}]pan=stereo|{pan},adelay={delay * (index + 1):g}:all=1,"
                    f"volume={gain:.8f}[tap{index}]")
    outputs = "".join(f"[tap{i}]" for i in range(len(gains)))
    return (f"[{source}]asplit={len(gains) + 1}[dryecho]{inputs};" + ";".join(taps) +
            f";[dryecho]{outputs}amix=inputs={len(gains) + 1}:duration=first:normalize=0[{output}]")


def _binaural(source: str, output: str, period: float, mix: float) -> str:
    # Four modeled directions, blended at sample rate. The far ear gets a smaller,
    # darker signal ~0.6 ms later. Rear cues also roll off and notch high frequencies.
    # shortcut: generic head model; use measured HRTFs for better front/rear localization.
    rate = 2 * pi / period
    directions = (
        "anull",
        "pan=stereo|FL=0.43*FL+0.17*FR|FR=0.35*FL+0.65*FR,"
        "highshelf=f=1800:g=-10:c=FL,adelay=29S|0S",
        "highshelf=f=2300:g=-6,equalizer=f=5200:t=q:w=1.2:g=-5",
        "pan=stereo|FL=0.65*FL+0.35*FR|FR=0.17*FL+0.43*FR,"
        "highshelf=f=1800:g=-10:c=FR,adelay=0S|29S",
    )
    paths = []
    for index, chain in enumerate(directions):
        weight = f"max(0,cos({rate:.9f}*t-{index * pi / 2:.9f}))^2"
        paths.append(f"[direction{index}]{chain},aeval='val(0)*{weight}|val(1)*{weight}'[ear{index}]")
    return (
        f"[{source}]asplit=5[rotationdry][direction0][direction1][direction2][direction3];"
        + ";".join(paths)
        + f";[rotationdry][ear0][ear1][ear2][ear3]amix=inputs=5:duration=first:normalize=0:"
        f"weights='{1 - mix:g} {mix:g} {mix:g} {mix:g} {mix:g}'[{output}]"
    )


def audio_effect_graph(
    effects: AudioEffectSettings | None, source: str, output: str,
    duration: float, *, input_filter: str | None = None,
) -> str:
    """A labeled filter graph for the whole mix, or an empty string when bypassed."""
    stages = []
    current = "fx_input"
    for key, values in normalize_audio_effects(effects).items():
        if not values["enabled"]:
            continue
        next_label = f"fx_{key}"
        mix = values.get("mix", 0) / 100
        if key == "stereo_width" and values["width"] != 100:
            graph = f"[{current}]stereotools=slev={values['width'] / 100:g}[{next_label}]"
        elif key == "chorus" and mix:
            graph = (f"[{current}]chorus=1:{1 / (1 + mix):.8f}:19|27:{mix * 0.6:g}|{mix * 0.4:g}:"
                     f"{values['rate']:g}|{values['rate'] * 1.19:g}:"
                     f"{values['depth']:g}|{values['depth'] * 0.8:g}[{next_label}]")
        elif key == "auto_pan" and values["strength"]:
            graph = f"[{current}]{_pan(values['period'], values['strength'])}[{next_label}]"
        elif key == "eight_d" and (values["strength"] or values["space"]):
            graph = f"[{current}]{_pan(values['period'], values['strength'])}[{next_label}]"
            if values["space"]:
                graph += ";" + _reverb(next_label, "fx_eight_d_room", "8d", 0.45, values["space"] / 100)
                next_label = "fx_eight_d_room"
        elif key == "binaural" and mix:
            graph = _binaural(current, next_label, values["period"], mix)
        elif key == "ping_pong" and mix:
            graph = _ping_pong(current, next_label, values["delay"], values["feedback"] / 100, mix)
        elif key == "reverb" and mix:
            graph = _reverb(current, next_label, "room", values["decay"], mix)
        else:
            continue
        stages.append(graph)
        current = next_label
    if not stages:
        return ""

    # Filter tails must fit the same timeline used for video, chapters, and lyrics.
    samples = round(duration * 48000)
    prefix = f"{input_filter}," if input_filter else ""
    return ";".join((
        f"[{source}]{prefix}aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
        "asetpts=N/SR/TB[fx_input]",
        *stages,
        f"[{current}]alimiter=limit={10 ** (TRUE_PEAK_DBTP / 20):.4f}:attack=3:release=60:latency=1:level=0,"
        f"apad=whole_len={samples},atrim=end_sample={samples}[{output}]",
    ))
