"""Relative short motions on the existing dynamic-layer FFmpeg adapter."""
from copy import deepcopy
from dataclasses import dataclass

from app.animation.curves import AnimationPose
from app.animation.custom_motion import CustomMotion, MOTION_FIELDS, motion_curves
from app.animation.keyframes import curve_maximum


def motion_windows(source, plan, sources=()):
    from app.timeline.render_plan import visual_segments
    if source.timeline_start > 0 or source.timeline_duration > 0:
        return ((source.timeline_start,
                 source.timeline_start + source.timeline_duration if source.timeline_duration > 0 else plan.duration_seconds,
                 source.animation_in_duration,
                 source.animation_out_duration if source.timeline_duration > 0 else 0),)
    sources = tuple(s for s in sources if s.visible) or (source,)
    result = []
    for segment in visual_segments(plan):
        window = plan.presentation.windows[segment.window_index]
        rate = window.playback_rate
        span = (window.timeline_end - window.timeline_start) * rate
        # These are the same short-track caps as ExportTimelinePlanner. All
        # project sources participate so another source cannot change timing
        # in Canvas while leaving the dynamic export layer on a different clock.
        intro_cap = min(span / 2, max((s.animation_in_duration for s in sources if s.animation_in != "none"), default=0))
        outro_cap = min((span - intro_cap) / 2, max((s.animation_out_duration for s in sources if s.animation_out != "none"), default=0))
        intro = (segment.mix_in_seconds if source.animation_fit_mix and segment.mix_in_seconds > 0 else source.animation_in_duration) if segment.mixed_in else min(source.animation_in_duration, intro_cap) / rate
        outro = (segment.mix_out_seconds if source.animation_fit_mix and segment.mix_out_seconds > 0 else source.animation_out_duration) if segment.mixed_out else min(source.animation_out_duration, outro_cap) / rate
        result.append((segment.start, segment.end, intro, outro))
    return tuple(result)


@dataclass(frozen=True)
class CustomMotionRenderBinding:
    source: object
    windows: tuple
    output_scale: float = 1.0

    @classmethod
    def build(cls, source, plan, output_scale=1, sources=()):
        return cls(deepcopy(source), motion_windows(source, plan, sources), output_scale)

    def motion(self, entering):
        data = self.source.custom_intro_keyframes if entering else self.source.custom_outro_keyframes
        return data or CustomMotion.default(entering)

    def pose(self, seconds):
        source = self.source
        if not self.windows:
            return AnimationPose()
        window = next((w for w in reversed(self.windows) if seconds >= w[0]), self.windows[0])
        start, end, intro, outro = window
        if source.animation_in == "custom" and intro > 0 and seconds < start + intro:
            return self.motion(True).at(max(0, min(1, (seconds - start) / intro)))
        if source.animation_out == "custom" and outro > 0 and seconds >= end - outro:
            return self.motion(False).at(max(0, min(1, (seconds - end + outro) / outro)))
        return self.motion(True).at(1) if source.animation_in == "custom" else AnimationPose()

    def values(self, seconds):
        pose, source = self.pose(seconds), self.source
        return dict(x=source.x + pose.dx, y=source.y + pose.dy, scale=source.scale * pose.scale,
                    rotation=source.rotation + pose.rotation, opacity=source.opacity * pose.opacity)

    @property
    def maximum_scale(self):
        return self.source.scale * max(1, *(min(10, curve_maximum(motion_curves(self.motion(entering))["scale"]))
            for entering, style in ((True, self.source.animation_in), (False, self.source.animation_out)) if style == "custom"))

    def expression(self, name, clock="t"):
        from app.animation.keyframe_render import curve_expression
        source = self.source
        field = {"x": "dx", "y": "dy"}.get(name, name)
        default = str(MOTION_FIELDS[field][2])
        def curve(entering, progress):
            low, high, _ = MOTION_FIELDS[field]
            value = curve_expression(motion_curves(self.motion(entering))[field], progress, default)
            return f"clip({value},{low},{high})"
        def segment(index):
            start, end, intro, outro = self.windows[index]
            value = f"{getattr(self.motion(True).at(1),field):.17g}" if source.animation_in == "custom" else default
            if source.animation_out == "custom" and outro > 0:
                progress = f"clip(({clock}-{end-outro:.17g})/{outro:.17g},0,1)"
                value = f"if(gte({clock},{end-outro:.17g}),{curve(False,progress)},{value})"
            if source.animation_in == "custom" and intro > 0:
                progress = f"clip(({clock}-{start:.17g})/{intro:.17g},0,1)"
                value = f"if(lt({clock},{start+intro:.17g}),{curve(True,progress)},{value})"
            return value
        def tree(low, high):
            if low == high:
                return segment(low)
            middle = (low + high + 1) // 2
            return f"if(lt({clock},{self.windows[middle][0]:.17g}),{tree(low,middle-1)},{tree(middle,high)})"
        relative = tree(0, len(self.windows) - 1) if self.windows else default
        base = getattr(source, name)
        return f"({base:.17g}{'*' if name in {'scale', 'opacity'} else '+'}({relative}))"
