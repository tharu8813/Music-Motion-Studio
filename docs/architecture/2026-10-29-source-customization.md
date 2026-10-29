# Shared source customization

All 17 source types share `flip_horizontal`, `flip_vertical`, `frame_width`,
`frame_color` (Qt RGB/ARGB), and `frame_inset`. Defaults leave existing projects
unchanged. `Source.from_dict` validates flags, finite numeric ranges and colors;
normal project serialization and duplication retain the settings.

The Inspector's shared `MotionSection` supplies these appearance controls, color
editing, mixed-selection bindings and bilingual help. Five frame design presets
change only frame settings. Clearing a frame retains other appearance settings.

Canvas renderers mirror source content inside the shared painter save/restore;
selection handles keep their normal placement. The frame is painted inside the
source before restoring the painter and follows source opacity and masks. Width
and inset are clamped for small sources. Shape repaint bounds account for mirrored
shadow offsets.

Reactive export uses the same decoration painter. Export translation scales
frame dimensions with source and render scale. Video export uses `hflip`, `vflip`
and `drawbox` before opacity/rotation, preserving the independent frame when video
corners are rounded. Neutral settings add no FFmpeg filters.

Seven additional shape choices reuse shared mask geometry. Four loop motions
(`orbit`, `shake`, `nod`, `blink`) use the shared deterministic animation curves,
so editor animation previews, playback capture and reactive export share timing.
Video retains its existing None/Fade-only animation support and disabled loop
choices; common decoration is supported on video too.

Verification covers project/legacy round trips, malformed saved settings, every
source's Inspector and Canvas frame, mixed selection, shape silhouettes, mirrored
repaint bounds, reactive export and actual FFmpeg output including rounded video.
