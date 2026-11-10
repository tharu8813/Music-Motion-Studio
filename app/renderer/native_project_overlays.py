"""Detached project overlay definitions matching ExportOrchestrator.visualizers."""
from app.models.source import SourceType
from app.animation.keyframe_render import KeyframeRenderBinding
from app.preview.album_art import adjust_personal_color, extract_track_personal_color
from app.renderer.ffmpeg_renderer import VisualizerOverlay
from app.timeline.compiler import compile_playlist
from app.utils.visualizer_geometry import visualizer_export_rect

def project_visualizers(
    project, tracks: list, render_scale: float = 1.0, compiled_plan=None,
) -> list[VisualizerOverlay]:
    """Translate visible, axis-aligned Canvas visualizers into Python-rendered overlays.

    ``render_scale`` (>= 1.0) maps Canvas coordinates onto the export's final
    pixel grid so an overlay stays aligned with the up-rendered base stream.
    """
    overlays: list[VisualizerOverlay] = []
    visualizer_sources = [
        source for source in project.sources
        if source.source_type in {
            SourceType.AUDIO_VISUALIZER, SourceType.AUDIO_WAVEFORM,
            SourceType.AUDIO_LEVEL_METER, SourceType.PARTICLE_OVERLAY,
        } and source.visible
    ]
    active_tracks = list(tracks)
    track_personal_colors = (
        tuple(
            extract_track_personal_color(track.file_path, track.cover_path)
            for track in active_tracks
        )
        if any(source.personal_color_enabled for source in visualizer_sources)
        else ()
    )
    for source in visualizer_sources:
        kind = {
            SourceType.AUDIO_VISUALIZER: "visualizer",
            SourceType.AUDIO_WAVEFORM: "waveform",
            SourceType.AUDIO_LEVEL_METER: "level_meter",
            SourceType.PARTICLE_OVERLAY: "particles",
        }[source.source_type]
        overlay_x, overlay_y, overlay_width, overlay_height = visualizer_export_rect(
            source, render_scale,
        )
        overlays.append(VisualizerOverlay(
            keyframe_binding=KeyframeRenderBinding.build(
                source, compiled_plan or compile_playlist(active_tracks), render_scale, sources=project.sources),
            x=overlay_x,
            y=overlay_y,
            width=overlay_width,
            height=overlay_height,
            style=source.visualizer_style,
            color=source.fill_color,
            personal_colors=(
                tuple(
                    adjust_personal_color(
                        color,
                        source.fill_color,
                        brightness=source.personal_color_brightness,
                        saturation=source.personal_color_saturation,
                        hue_shift=source.personal_color_hue_shift,
                        strength=source.personal_color_strength,
                    )
                    for color in track_personal_colors
                )
                if source.personal_color_enabled else ()
            ),
            opacity=1.0 if source.keyframes.animated or source.animation_in == "custom" or source.animation_out == "custom" else source.opacity,
            bar_count=source.visualizer_bars,
            line_width=source.visualizer_line_width * render_scale,
            sensitivity=source.visualizer_sensitivity,
            reactivity=source.visualizer_reactivity,
            noise_gate=source.visualizer_noise_gate,
            min_level=source.visualizer_min_level,
            max_level=source.visualizer_max_level,
            attack=source.visualizer_attack,
            release=source.visualizer_release,
            smoothing=source.visualizer_smoothing,
            curve=source.visualizer_curve,
            kind=kind,
            effect_style=(source.waveform_style if kind == "waveform" else
                          source.level_meter_style if kind == "level_meter" else
                          source.particle_style if kind == "particles" else source.visualizer_style),
            density=source.particle_density,
            speed=source.particle_speed,
            level_meter_mode=(
                "stereo" if source.level_meter_mode == "led" else source.level_meter_mode
            ),
            level_meter_style=(
                "led" if source.level_meter_mode == "led" else source.level_meter_style
            ),
            level_meter_orientation=source.level_meter_orientation,
            level_meter_sensitivity=source.level_meter_sensitivity,
            level_meter_attack=source.level_meter_attack,
            level_meter_release=source.level_meter_release,
            level_meter_min_level=source.level_meter_min_level,
            level_meter_max_level=source.level_meter_max_level,
            level_meter_segments=source.level_meter_segments,
            level_meter_gap=source.level_meter_gap * render_scale,
            level_meter_show_peak=source.level_meter_show_peak,
            level_meter_peak_hold=source.level_meter_peak_hold,
            level_meter_peak_decay=source.level_meter_peak_decay,
            level_meter_track_color=source.level_meter_track_color,
            level_meter_low_color=source.level_meter_low_color,
            level_meter_mid_color=source.level_meter_mid_color,
            level_meter_high_color=source.level_meter_high_color,
            particle_min_size=source.particle_min_size * render_scale,
            particle_max_size=source.particle_max_size * render_scale,
            particle_opacity=source.particle_opacity,
            particle_direction=source.particle_direction,
            particle_drift=source.particle_drift,
            particle_twinkle=source.particle_twinkle,
            particle_glow=source.particle_glow,
            particle_secondary_color=source.particle_secondary_color,
            particle_seed=source.particle_seed,
            rotation=source.rotation,
            z_index=source.z_index,
            timeline_start=source.timeline_start,
            timeline_duration=source.timeline_duration,
            animation_in="none" if source.animation_in == "custom" else source.animation_in,
            animation_out="none" if source.animation_out == "custom" else source.animation_out,
            animation_in_duration=source.animation_in_duration,
            animation_out_duration=source.animation_out_duration,
            animation_fit_mix=source.animation_fit_mix,
            loop_motion=source.loop_motion,
            loop_motion_period=source.loop_motion_period,
            loop_motion_amount=source.loop_motion_amount,
            mask_shape=source.mask_shape,
            inner_radius=source.visualizer_inner_radius,
            flip_horizontal=source.flip_horizontal,
            flip_vertical=source.flip_vertical,
            frame_width=source.frame_width * source.scale * render_scale,
            frame_color=source.frame_color,
            frame_inset=source.frame_inset * source.scale * render_scale,
        ))
    # FFmpeg overlays later inputs on top.  Preserve the Canvas stacking
    # order when two reactive sources overlap.
    return sorted(overlays, key=lambda overlay: (overlay.z_index, overlay.y, overlay.x))
