"""The FFmpeg adapter for the same curves and visual ownership used by Qt."""
from copy import deepcopy
from dataclasses import dataclass, replace
from math import ceil, hypot

from app.animation.keyframes import evaluate, property_value, PROPERTIES, BEZIER_ITERATIONS, curve_maximum


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
    def build(cls, source, plan, output_scale=1.0, *, sources=()):
        from app.animation.custom_motion import has_custom_motion
        if has_custom_motion(source):
            from app.animation.custom_motion_render import CustomMotionRenderBinding
            return CustomMotionRenderBinding.build(source, plan, output_scale, sources)
        return cls(deepcopy(source), keyframe_windows(plan), output_scale) if source.keyframes.animated else None

    def values(self, seconds):
        window = next((w for w in reversed(self.windows) if w.timeline_start <= seconds < w.timeline_end), None)
        return evaluate(self.source, seconds, window.track_id if window else "",
                        window.source_time_at_start + (seconds - window.timeline_start) * window.playback_rate if window else None)

    def expression(self, name, clock="t"):
        source, animation = self.source, self.source.keyframes
        base = f"{property_value(source, name):.12g}"
        spec = PROPERTIES[name]
        def bounded(points, clock):
            value = curve_expression(points, clock, base)
            return f"clip({value},{spec.minimum:.12g},{spec.maximum:.12g})" if points else base
        if animation.mode == "global":
            return bounded(animation.global_curves.get(name, []), clock)
        entries = []
        for window in self.windows:
            if animation.mode == "single_track" and window.track_id != animation.target_track_id:
                continue
            local = f"({window.source_time_at_start:.17g}+({clock}-{window.timeline_start:.17g})*{window.playback_rate:.17g})"
            points = animation.curves(window.track_id).get(name, [])
            if not points:
                continue
            value = bounded(points, local)
            entries.append((window, value))
        def tree(low, high):
            if low == high:
                window, value = entries[low]
                return f"if(gte({clock},{window.timeline_start:.17g})*lt({clock},{window.timeline_end:.17g}),{value},{base})"
            middle = (low + high + 1) // 2
            return f"if(lt({clock},{entries[middle][0].timeline_start:.17g}),{tree(low,middle-1)},{tree(middle,high)})"
        return tree(0, len(entries) - 1) if entries else base


def curve_expression(points, clock, base):
    if not points:
        return base
    def segment(index):
        if index == len(points) - 1:
            return f"{points[index].value:.17g}"
        left, right = points[index], points[index + 1]
        t = f"clip(({clock}-{left.time:.17g})/{right.time-left.time:.17g},0,1)"
        if left.interpolation == "hold":
            weight = "0"
        elif left.interpolation == "ease_in":
            weight = f"pow({t},3)"
        elif left.interpolation == "ease_out":
            weight = f"(1-pow(1-{t},3))"
        elif left.interpolation == "ease_in_out":
            weight = f"if(lt({t},0.5),4*pow({t},3),1-pow(-2*{t}+2,3)/2)"
        elif left.interpolation == "bezier":
            weight = bezier_expression(t, left.bezier)
        else:
            weight = t
        return f"({left.value:.17g}+({right.value-left.value:.17g})*({weight}))"
    def tree(low, high):
        if low == high:
            return segment(low)
        middle = (low + high + 1) // 2
        return f"if(lt({clock},{points[middle].time:.17g}),{tree(low,middle-1)},{tree(middle,high)})"
    # FFmpeg bounds expression recursion. A linear chain of if() branches
    # fails with a few hundred keys even when passed through a script file.
    return f"if(lt({clock},{points[0].time:.17g}),{points[0].value:.17g},{tree(0,len(points)-1)})"


def bezier_expression(progress, handles):
    """The same bounded bisection as bezier_progress; registers 7–9 stay free."""
    x1, y1, x2, y2 = handles
    def coordinate(first, second):
        return f"(3*pow(1-ld(3),2)*ld(3)*{first:.12g}+3*(1-ld(3))*pow(ld(3),2)*{second:.12g}+pow(ld(3),3))"
    solve = (f"st(1,0);st(2,1);st(4,0);while(lt(ld(4),{BEZIER_ITERATIONS}),"
             f"st(3,(ld(1)+ld(2))/2);if(lt({coordinate(x1,x2)},ld(0)),st(1,ld(3)),st(2,ld(3)));"
             f"st(4,ld(4)+1));st(3,(ld(1)+ld(2))/2);{coordinate(y1,y2)}")
    return f"(st(0,{progress});if(lte(ld(0),0),0,if(gte(ld(0),1),1,({solve}))))"


def cached_frame_expression(expression, clock="T"):
    # geq evaluates pixels independently. Solve time-only Bezier curves once
    # per timestamp per expression/thread, including a correctly initialized t=0.
    return (f"if(eq(ld(7),1)*eq(ld(8),{clock}),ld(9),"
            f"(st(7,1);st(8,{clock});st(9,{expression})))")


def ffmpeg_transform(graph, layer_input, binding, width, height, tag):
    """Keep all motion inside a fixed rotated envelope; overlay tracks its centre."""
    source, factor = binding.source, binding.output_scale
    scale = binding.expression("scale")
    ratio = f"(({scale})/{source.scale or 1:.12g})"
    max_scale = getattr(binding, "maximum_scale", None)
    if max_scale is None:
        animation = source.keyframes
        curves = ([animation.global_curves] if animation.mode == "global" else
                  [animation.curves()] if animation.mode == "single_track" else list(animation.per_track_curves.values()))
        max_scale = min(PROPERTIES["scale"].maximum, max([source.scale, *[curve_maximum(curve.get("scale", [])) for curve in curves]]))
    side = max(2, ceil(hypot(width, height) * max_scale / (source.scale or 1)) + 4)
    transformed = f"[kfgeometry{tag}]"
    angle = f"({binding.expression('rotation')})*PI/180"
    # "none" disables background filling and can retain earlier rotated pixels.
    graph.append(f"{layer_input}scale=w='max(1,round({width}*{ratio}))':h='max(1,round({height}*{ratio}))':eval=frame,"
                 f"rotate='{angle}':ow={side}:oh={side}:fillcolor=black@0{transformed}")
    color, alpha, masked, output = (f"[kf{part}{tag}]" for part in ("color", "alpha", "mask", "out"))
    graph.append(f"{transformed}format=gbrap,split=2{color}{alpha}")
    opacity = binding.expression('opacity', 'T')
    if "while(" in opacity:
        opacity = cached_frame_expression(opacity)
    graph.append(f"{alpha}alphaextract,geq=lum='lum(X,Y)*({opacity})'{masked}")
    graph.append(f"{color}{masked}alphamerge{output}")
    x = f"(({binding.expression('x')})+{source.width / 2:.12g})*{factor:.12g}-overlay_w/2"
    y = f"(({binding.expression('y')})+{source.height / 2:.12g})*{factor:.12g}-overlay_h/2"
    return output, x, y
