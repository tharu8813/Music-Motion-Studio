"""FFmpeg filter_complex graph construction, split out of FFmpegRenderer.

Pure string-building: no subprocess calls, no file I/O, no Qt. Lifted
verbatim from FFmpegRenderer's private static methods of the same name
(without the leading underscore) -- FFmpegRenderer keeps thin delegating
staticmethods so every existing call site (including tests that call
FFmpegRenderer._layered_filter_graph(...) etc. directly) keeps working
unchanged.
"""

from __future__ import annotations

import os
from math import cos, radians, sin
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.renderer.ffmpeg_renderer import (
        VideoClipOverlay, VideoFileInput, VisualizerOverlay,
    )


def filter_color(value: str) -> str:
    """Convert Qt RGB/AARRGGBB into FFmpeg RGB/RRGGBBAA."""
    color = value.strip().removeprefix("#")
    if len(color) in {6, 8} and all(character in "0123456789abcdefABCDEF" for character in color):
        return f"0x{color[2:]}{color[:2]}" if len(color) == 8 else f"0x{color}"
    return "0x000000"


def output_scaling_filter(fps: int, output_width: int, output_height: int,
                           include_fps: bool = True) -> str:
    """Scale without stretching the authored canvas and pad any aspect mismatch."""
    prefix = f"fps={fps}," if include_fps else ""
    return (
        f"{prefix}scale={output_width}:{output_height}:"
        "force_original_aspect_ratio=decrease,"
        f"pad={output_width}:{output_height}:(ow-iw)/2:(oh-ih)/2:color=black"
    )


def video_fade_expression(clip: "VideoClipOverlay") -> str:
    """Use the same cubic fade and absolute visual windows as Canvas."""
    clock = f"(T+{clip.timeline_start:.8f})"
    result = "1" if clip.animation_out == "none" else "0"
    for start, end, intro, outro in reversed(clip.animation_windows):
        if end <= clip.timeline_start or start >= clip.timeline_start + clip.duration_seconds:
            continue
        value = "1"
        if clip.animation_out == "fade" and outro > 0:
            progress = f"clip(({clock}-{end - outro:.8f})/{outro:.8f},0,1)"
            ease = f"if(lt({progress},0.5),4*pow({progress},3),1-pow(-2*{progress}+2,3)/2)"
            value = f"(1-({ease}))"
        if clip.animation_in == "fade" and intro > 0:
            progress = f"clip(({clock}-{start:.8f})/{intro:.8f},0,1)"
            ease = f"if(lt({progress},0.5),4*pow({progress},3),1-pow(-2*{progress}+2,3)/2)"
            value = f"if(lt({clock},{start + intro:.8f}),{ease},{value})"
        result = f"if(between({clock},{start:.8f},{end:.8f}),{value},{result})"
    return result


def python_visualizer_filter_graph(visualizers: list["VisualizerOverlay"], fps: int,
                                    output_width: int, output_height: int) -> str:
    """Composite pre-rendered Python alpha layers; no FFmpeg analyzer filters are used."""
    # The Canvas uses a concat manifest of still images.  Convert that stream to
    # constant frame rate *before* overlay framesync, otherwise framesync outputs
    # only one frame per still image and the video visibly stutters.
    graph: list[str] = [
        f"[0:v]fps={fps}:start_time=0,settb=AVTB,setpts=N/({fps}*TB)[base]"
    ]
    current = "[base]"
    for index, overlay in enumerate(visualizers):
        output = "[composited]" if index == len(visualizers) - 1 else f"[layer{index}]"
        # Inputs 0 and 1 are Canvas and playlist audio; Python layer videos begin at 2.
        layer_input = f"[{index + 2}:v]"
        rotation = float(overlay.rotation) % 360.0
        x, y = overlay.x, overlay.y
        if rotation:
            radians_value = radians(rotation)
            rotated_width = abs(overlay.width * cos(radians_value)) + abs(overlay.height * sin(radians_value))
            rotated_height = abs(overlay.width * sin(radians_value)) + abs(overlay.height * cos(radians_value))
            x = round(overlay.x - (rotated_width - overlay.width) / 2.0)
            y = round(overlay.y - (rotated_height - overlay.height) / 2.0)
            rotated_label = f"[rotated{index}]"
            graph.append(
                f"{layer_input}rotate={radians_value:.12f}:"
                f"ow=rotw({radians_value:.12f}):"
                f"oh=roth({radians_value:.12f}):fillcolor=none{rotated_label}"
            )
            layer_input = rotated_label
        graph.append(f"{current}{layer_input}overlay={x}:{y}:eof_action=pass{output}")
        current = output
    # Overlay coordinates are canvas coordinates.  Scale only after compositing
    # so a source at (100, 100) stays at that location on the artboard.
    graph.append(
        f"{current}{output_scaling_filter(fps, output_width, output_height, include_fps=False)}[vout]"
    )
    return ";".join(graph)


def video_input_plan(
    video_clips: list["VideoClipOverlay"],
) -> tuple[list["VideoFileInput"], list[int]]:
    """Deduplicate matching files while retaining per-occurrence filter branches."""
    from app.renderer.ffmpeg_renderer import VideoFileInput

    groups: list[dict[str, object]] = []
    group_slots: dict[tuple[str, float], int] = {}
    clip_slots: list[int] = []
    for clip in video_clips:
        resolved = clip.path.resolve()
        key = (
            os.path.normcase(str(resolved)),
            round(max(0.0, clip.media_start_seconds), 6),
        )
        slot = group_slots.get(key)
        raw_duration = max(
            0.000001,
            clip.duration_seconds * max(0.05, clip.speed),
        )
        if slot is None:
            slot = len(groups)
            group_slots[key] = slot
            groups.append({
                "path": resolved,
                "media_start": key[1],
                "duration": raw_duration,
                "loop": bool(clip.loop_input),
            })
        else:
            group = groups[slot]
            group["duration"] = max(float(group["duration"]), raw_duration)
            group["loop"] = bool(group["loop"]) or bool(clip.loop_input)
        clip_slots.append(slot)
    return [
        VideoFileInput(
            path=group["path"],  # type: ignore[arg-type]
            media_start_seconds=float(group["media_start"]),
            duration_seconds=float(group["duration"]),
            loop_input=bool(group["loop"]),
        )
        for group in groups
    ], clip_slots


def layered_filter_graph(visualizers: list["VisualizerOverlay"],
                          static_layers: list[
                              tuple[float, "os.PathLike"]
                              | tuple[float, "os.PathLike", str]
                              | tuple[float, "os.PathLike", str, int, int]
                          ], fps: int,
                          output_width: int, output_height: int,
                          video_clips: list["VideoClipOverlay"] | None = None,
                          video_input_slots: list[int] | None = None) -> str:
    """Interleave reactive video and transparent static Z bands in Canvas order."""
    video_clips = video_clips or []
    video_file_inputs, planned_slots = video_input_plan(video_clips)
    if video_input_slots is None:
        video_input_slots = planned_slots
    if len(video_input_slots) != len(video_clips):
        raise ValueError("Video input slots do not match scheduled clips.")
    graph: list[str] = [f"[0:v]fps={fps}:start_time=0,settb=AVTB,setpts=N/({fps}*TB)[base]"]
    entries: list[tuple[float, int, str]] = []
    # Inputs 0/1 are base canvas and audio. Dynamic video inputs precede
    # static concat inputs, preserving their independent source timing.
    entries.extend((overlay.z_index, index, "dynamic") for index, overlay in enumerate(visualizers))
    entries.extend((clip.z_index, index, "video") for index, clip in enumerate(video_clips))
    video_offset = 2 + len(visualizers)
    static_offset = video_offset + len(video_file_inputs)
    video_layer_inputs = [""] * len(video_clips)
    for slot in range(len(video_file_inputs)):
        consumers = [
            clip_index for clip_index, input_slot in enumerate(video_input_slots)
            if input_slot == slot
        ]
        input_label = f"[{video_offset + slot}:v]"
        if len(consumers) == 1:
            video_layer_inputs[consumers[0]] = input_label
            continue
        outputs = [f"[vsrc{slot}_{branch}]" for branch in range(len(consumers))]
        graph.append(f"{input_label}split={len(outputs)}{''.join(outputs)}")
        for clip_index, output in zip(consumers, outputs, strict=True):
            video_layer_inputs[clip_index] = output
    entries.extend(
        (layer[0], index, "static")
        for index, layer in enumerate(static_layers)
    )
    current = "[base]"
    # A transparent static band is tagged with the Z value of the dynamic
    # layer immediately below it.  Compare the layer kind before its input
    # index so that the dynamic layer is always drawn first at that shared
    # boundary.  Comparing input indices first made a later particle/noise
    # layer cover a static foreground whenever an earlier empty band had
    # been omitted.
    ordered_entries = sorted(
        entries,
        key=lambda entry: (
            entry[0], 0 if entry[2] in {"dynamic", "video"} else 1, entry[1],
        ),
    )
    for order, (_z_value, index, kind) in enumerate(ordered_entries):
        output = "[composited]" if order == len(entries) - 1 else f"[zlayer{order}]"
        if kind == "dynamic":
            overlay = visualizers[index]
            layer_input = f"[{index + 2}:v]"
            rotation = float(overlay.rotation) % 360.0
            x, y = overlay.x, overlay.y
            if rotation:
                angle = radians(rotation)
                rotated_width = abs(overlay.width * cos(angle)) + abs(overlay.height * sin(angle))
                rotated_height = abs(overlay.width * sin(angle)) + abs(overlay.height * cos(angle))
                x = round(x - (rotated_width - overlay.width) / 2.0)
                y = round(y - (rotated_height - overlay.height) / 2.0)
                rotated = f"[zrot{order}]"
                graph.append(
                    f"{layer_input}rotate={angle:.12f}:"
                    f"ow=rotw({angle:.12f}):"
                    f"oh=roth({angle:.12f}):fillcolor=none{rotated}"
                )
                layer_input = rotated
            graph.append(f"{current}{layer_input}overlay={x}:{y}:eof_action=pass{output}")
        elif kind == "video":
            clip = video_clips[index]
            source_input = video_layer_inputs[index]
            layer_input = f"[vclip{order}]"
            padding = ""
            if clip.fit_mode == "stretch":
                geometry = f"scale={clip.width}:{clip.height}"
            elif clip.fit_mode == "contain":
                fill_color = filter_color(clip.fill_color)
                geometry = f"scale={clip.width}:{clip.height}:force_original_aspect_ratio=decrease"
                padding = (
                    f"format=rgba,pad={clip.width}:{clip.height}:(ow-iw)/2:(oh-ih)/2:"
                    f"color={fill_color}"
                )
            else:
                geometry = (
                    f"scale={clip.width}:{clip.height}:force_original_aspect_ratio=increase,"
                    f"crop={clip.width}:{clip.height}"
                )
            filters = [
                f"{source_input}trim=duration="
                f"{clip.duration_seconds * max(0.05, clip.speed):.8f},"
                f"settb=AVTB,setpts=(PTS-STARTPTS)/{max(0.05, clip.speed):.8f}",
                geometry,
            ]
            if clip.brightness != 0.0 or clip.contrast != 0.0 or clip.saturation != 1.0:
                # Canvas stores brightness and contrast as percentages.
                filters.append(
                    f"eq=brightness={max(-1.0, min(1.0, clip.brightness / 100.0)):.4f}:"
                    f"contrast={max(0.0, min(2.0, 1.0 + clip.contrast / 100.0)):.4f}:"
                    f"saturation={max(0.0, min(3.0, clip.saturation)):.4f}"
                )
            if clip.grayscale:
                filters.append("hue=s=0")
            if clip.blur > 0.01:
                filters.append(f"boxblur={min(40.0, clip.blur):.3f}:1")
            # Add padding after color effects so its RGBA color remains exact.
            if padding:
                filters.append(padding)
            radius = max(
                0.0,
                min(float(clip.border_radius), clip.width / 2.0, clip.height / 2.0),
            )
            alpha_expression = "lum(X,Y)"
            if radius >= 0.5:
                dx = (
                    f"max(max({radius:.4f}-X,X-(W-1-{radius:.4f})),0)"
                )
                dy = (
                    f"max(max({radius:.4f}-Y,Y-(H-1-{radius:.4f})),0)"
                )
                alpha_expression = (
                    f"if(lte(pow({dx},2)+pow({dy},2),"
                    f"pow({radius:.4f},2)),lum(X,Y),0)"
                )
            filters.append("format=rgba")
            if (clip.opacity < 1.0 or radius >= 0.5
                    or clip.animation_in != "none" or clip.animation_out != "none"):
                filters.append(f"colorchannelmixer=aa={max(0.0, min(1.0, clip.opacity)):.4f}")
            if clip.animation_in != "none" or clip.animation_out != "none":
                opacity = video_fade_expression(clip)
                alpha_expression = f"({alpha_expression})*({opacity})"
            timing = f"setpts=PTS+{max(0.0, clip.timeline_start):.8f}/TB{layer_input}"
            if alpha_expression != "lum(X,Y)":
                # Evaluate the mask on one alpha plane instead of resampling all four RGBA planes.
                color, alpha, masked = f"[vcolor{order}]", f"[valpha{order}]", f"[vmask{order}]"
                graph.append(",".join(filters) + f",format=gbrap,split=2{color}{alpha}")
                graph.append(f"{alpha}alphaextract,geq=lum='{alpha_expression}'{masked}")
                graph.append(f"{color}{masked}alphamerge,{timing}")
            else:
                graph.append(",".join([*filters, timing]))
            rotation = float(clip.rotation) % 360.0
            x, y = clip.x, clip.y
            if rotation:
                angle = radians(rotation)
                rotated_width = (
                    abs(clip.width * cos(angle))
                    + abs(clip.height * sin(angle))
                )
                rotated_height = (
                    abs(clip.width * sin(angle))
                    + abs(clip.height * cos(angle))
                )
                x = round(x - (rotated_width - clip.width) / 2.0)
                y = round(y - (rotated_height - clip.height) / 2.0)
                rotated = f"[vrot{order}]"
                graph.append(
                    f"{layer_input}rotate={angle:.12f}:"
                    f"ow=rotw({angle:.12f}):"
                    f"oh=roth({angle:.12f}):fillcolor=none{rotated}"
                )
                layer_input = rotated
            graph.append(
                f"{current}{layer_input}overlay={x}:{y}:eof_action=pass:shortest=0{output}"
            )
        else:
            static_layer = static_layers[index]
            input_kind = static_layer[2] if len(static_layer) == 3 else "concat"
            if len(static_layer) >= 5:
                input_kind = static_layer[2]
                layer_x, layer_y = int(static_layer[3]), int(static_layer[4])
            else:
                layer_x, layer_y = 0, 0
            input_index = static_offset + index
            if input_kind == "alpha_pair":
                layer_input = f"[staticrgba{order}]"
                graph.append(
                    f"[{input_index}:v:0][{input_index}:v:1]"
                    f"alphamerge{layer_input}"
                )
            else:
                layer_input = f"[{input_index}:v]"
            graph.append(
                f"{current}{layer_input}overlay={layer_x}:{layer_y}:"
                f"eof_action=pass{output}"
            )
        current = output
    graph.append(f"{current}{output_scaling_filter(fps, output_width, output_height, include_fps=False)}[vout]")
    return ";".join(graph)
