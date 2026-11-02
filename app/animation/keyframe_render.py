"""The FFmpeg adapter for the same curves and visual ownership used by Qt."""
from copy import deepcopy
from dataclasses import dataclass, replace
from math import ceil, hypot

from app.animation.keyframes import evaluate


def keyframe_windows(plan):
    """One visual owner, with audio-local seconds, including silent-gap exclusion."""
    from app.timeline.render_plan import visual_segments
    result = []
    for segment in visual_segments(plan):
        window = plan.presentation.windows[segment.window_index]
        result.append(replace(window, timeline_start=segment.start,
            timeline_end=min(segment.end, window.timeline_end),
            source_time_at_start=window.source_time_at_start + (segment.start - window.timeline_start) * window.playback_rate))
    return tuple(result)


@dataclass(frozen=True)
class KeyframeRenderBinding:
    source: object
    windows: tuple
    output_scale: float = 1.0

    @classmethod
    def build(cls, source, plan, output_scale=1.0):
        return cls(deepcopy(source), keyframe_windows(plan), output_scale) if source.keyframes.animated else None

    def values(self, seconds):
        window = next((w for w in reversed(self.windows) if w.timeline_start <= seconds < w.timeline_end), None)
        return evaluate(self.source, seconds, window.track_id if window else "",
                        window.source_time_at_start + (seconds - window.timeline_start) * window.playback_rate if window else None)

    def expression(self, name, clock="t"):
        source, animation = self.source, self.source.keyframes
        base = f"{getattr(source, name):.12g}"
        if animation.mode == "global":
            return curve_expression(animation.global_curves.get(name, []), clock, base)
        result = base
        for window in reversed(self.windows):
            if animation.mode == "single_track" and window.track_id != animation.target_track_id:
                continue
            local = f"({window.source_time_at_start:.12g}+({clock}-{window.timeline_start:.12g})*{window.playback_rate:.12g})"
            points = animation.curves(window.track_id).get(name, [])
            if not points:
                continue
            value = curve_expression(points, local, base)
            result = f"if(gte({clock},{window.timeline_start:.12g})*lt({clock},{window.timeline_end:.12g}),{value},{result})"
        return result


def curve_expression(points, clock, base):
    if not points:
        return base
    result = f"{points[-1].value:.12g}"
    for left, right in reversed(list(zip(points, points[1:]))):
        t = f"clip(({clock}-{left.time:.12g})/{right.time-left.time:.12g},0,1)"
        if left.interpolation == "hold":
            weight = "0"
        elif left.interpolation == "ease_in":
            weight = f"pow({t},3)"
        elif left.interpolation == "ease_out":
            weight = f"(1-pow(1-{t},3))"
        elif left.interpolation == "ease_in_out":
            weight = f"if(lt({t},0.5),4*pow({t},3),1-pow(-2*{t}+2,3)/2)"
        else:
            weight = t
        value = f"({left.value:.12g}+({right.value-left.value:.12g})*({weight}))"
        result = f"if(lt({clock},{right.time:.12g}),{value},{result})"
    return f"if(lt({clock},{points[0].time:.12g}),{points[0].value:.12g},{result})"


def ffmpeg_transform(graph, layer_input, binding, width, height, tag):
    """Keep all motion inside a fixed rotated envelope; overlay tracks its centre."""
    source, factor = binding.source, binding.output_scale
    scale = binding.expression("scale")
    ratio = f"(({scale})/{source.scale or 1:.12g})"
    animation = source.keyframes
    curves = ([animation.global_curves] if animation.mode == "global" else
              [animation.curves()] if animation.mode == "single_track" else list(animation.per_track_curves.values()))
    max_scale = max([source.scale, *[p.value for curve in curves for p in curve.get("scale", [])]])
    side = max(2, ceil(hypot(width, height) * max_scale / (source.scale or 1)) + 4)
    transformed = f"[kfgeometry{tag}]"
    angle = f"({binding.expression('rotation')})*PI/180"
    graph.append(f"{layer_input}scale=w='max(1,round({width}*{ratio}))':h='max(1,round({height}*{ratio}))':eval=frame,"
                 f"rotate='{angle}':ow={side}:oh={side}:fillcolor=none{transformed}")
    color, alpha, masked, output = (f"[kf{part}{tag}]" for part in ("color", "alpha", "mask", "out"))
    graph.append(f"{transformed}format=gbrap,split=2{color}{alpha}")
    graph.append(f"{alpha}alphaextract,geq=lum='lum(X,Y)*({binding.expression('opacity', 'T')})'{masked}")
    graph.append(f"{color}{masked}alphamerge{output}")
    x = f"(({binding.expression('x')})+{source.width / 2:.12g})*{factor:.12g}-overlay_w/2"
    y = f"(({binding.expression('y')})+{source.height / 2:.12g})*{factor:.12g}-overlay_h/2"
    return output, x, y
