"""Cue motion and typography shared by Canvas preview and video export."""

from functools import lru_cache
from math import cos, pi, sin
import re
from typing import TYPE_CHECKING

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QTextLayout, QTextOption

from app.animation.lyrics import context_style_at_distance, cue_effect_pose, intro_exit_pose, role_style, stagger_progress
from app.animation.curves import music_reactive_pose
from app.canvas.renderers.base import device_pixel_ratio, paint_background, paint_selection_guide

if TYPE_CHECKING:
    from app.canvas.source_item import SourceItem

_ALIGNMENTS = {"left": Qt.AlignmentFlag.AlignLeft, "right": Qt.AlignmentFlag.AlignRight}
_VISIBLE = 0.004
_MIN_FIT = 0.55
_TRANSLATION_GAP_RATIO = 0.6


@lru_cache(maxsize=512)
def lyric_display_lines(text: str) -> tuple[str, ...]:
    return tuple(line for line in text.splitlines() if line.strip())


def render(item: "SourceItem", painter: QPainter, option: object, widget: object | None = None) -> None:
    rect, _fill, _pen = paint_background(item, painter)
    painter.drawRoundedRect(rect, item.source.border_radius, item.source.border_radius)
    paint_lines(item, painter, rect)
    paint_selection_guide(item, painter, rect)


def _mix(start: QColor, end: QColor, amount: float) -> QColor:
    return QColor.fromRgbF(*(a + (b - a) * amount for a, b in zip(start.getRgbF(), end.getRgbF())))


def _line_font_size(source, line: dict) -> float:
    size = float(line.get("font_size", source.font_size + float(line.get("font_size_offset", 0))))
    return max(8.0, min(120.0, size))


def _line_style(source, role: str, line: dict, karaoke_line=True) -> dict:
    style = role_style(source, role)
    style["color"] = (
        source.subtitle_accent_color if role == "current" and source.subtitle_accent_enabled
        and not source.subtitle_karaoke_enabled
        else line.get("color", source.outline_color)
    )
    if source.subtitle_karaoke_enabled and karaoke_line:
        style["color"] = source.subtitle_accent_color if role == "previous" else pending_color(source).name(QColor.NameFormat.HexArgb)
    style["size"] = _line_font_size(source, line) * float(style["scale"])
    style["font_weight"] = line.get("font_weight", source.font_weight)
    style["italic"] = line.get("italic", source.text_italic)
    return style


def lyric_line_overrides(source, style_index: int) -> dict:
    if style_index < 0:
        return source.subtitle_translation_style
    return source.subtitle_line_styles[style_index] if 0 <= style_index < len(source.subtitle_line_styles) else {}


def _display_line_style(source, role, overrides, style_index, karaoke=True):
    style = _line_style(source, role, overrides, karaoke)
    if style_index < 0:
        # Role motion/opacity still follows the cue, but source-line accent and
        # word highlighting must never override the translation's own typography.
        style["color"] = overrides.get("color", source.outline_color)
    return style


def pending_color(source) -> QColor:
    """Dim RGB channels, keeping opacity and the completed lyric's hue unchanged."""
    if source.subtitle_karaoke_pending_mode == "custom":
        return QColor(source.subtitle_karaoke_pending_color)
    color = QColor(source.subtitle_accent_color)
    brightness = source.subtitle_karaoke_pending_brightness
    return QColor.fromRgbF(color.redF() * brightness, color.greenF() * brightness,
                          color.blueF() * brightness, color.alphaF())


def lyric_wrap_width(source) -> float:
    if not source.subtitle_wrap_to_source:
        return source.subtitle_wrap_width
    if source.subtitle_flow_direction in {"left", "right"}:
        # Include the current cue as well as the previous/upcoming cues.
        slots = ((source.subtitle_context_lines if source.subtitle_context_lines >= 0 else 1)
                 + (source.subtitle_next_lines if source.subtitle_next_lines >= 0 else 1) + 1)
        return max(16.0, source.width / slots - source.subtitle_cue_spacing)
    return max(16.0, source.width - 24)


def lyric_wrapped_rows(item, text: str, style_index: int = 0) -> tuple[tuple[str, int], ...]:
    """Return whole-word rows and their character offsets in the original line.

    Measure at the largest cue role so a transition cannot change line breaks.
    Offsets keep TTML highlighting attached to the original text and timings.
    """
    source = item.source
    if not source.subtitle_auto_wrap:
        return ((text, 0),)
    width = lyric_wrap_width(source)
    overrides = lyric_line_overrides(source, style_index)
    # Wrapping depends on typography and role scales, not on cue motion,
    # colours or opacity. Check the cache before resolving any Qt fonts.
    size = _line_font_size(source, overrides)
    sizes = tuple(max(8, round(size * float(source.subtitle_role_styles.get(role, {}).get(
        "scale", source.subtitle_current_scale if role == "current" else 1.0,
    )))) for role in ("previous", "current", "next"))
    family = str(overrides.get("font_family", ""))
    weight = int(overrides.get("font_weight", source.font_weight))
    italic = bool(overrides.get("italic", source.text_italic))
    key = (text, width, sizes, family or source.font_family, weight, italic, item._typography_key())
    cache = item._lyric_wrap_cache
    if key in cache:
        return cache[key]
    fonts = tuple(item._lyric_font(size, weight, italic, family) for size in dict.fromkeys(sizes))
    words = list(re.finditer(r"\S+", text))
    rows = []
    if words:
        start, end = words[0].start(), words[0].end()
        for word in words[1:]:
            candidate = text[start:word.end()]
            if max(item._lyric_text_advance(font, candidate) for font in fonts) + 2 > width:
                rows.append((text[start:end], start))
                start = word.start()
            end = word.end()
        rows.append((text[start:end], start))
    result = tuple(rows) or ((text, 0),)
    if len(cache) >= 256:
        cache.pop(next(iter(cache)))
    cache[key] = result
    return result


@lru_cache(maxsize=128)
def _wrapped_row_layout(original_layout, origins, height, line_spacing, style_indices=(), cue_spacing=None):
    """Compact wraps and original-to-translation gaps within the same cue.

    Positions use normal lyric-row units, including fractional units for
    compact continuations. Cue centers and motion then share the same geometry.
    """
    continuation_step = max(1.0, height - max(0.0, line_spacing)) / height
    cue_spacing = line_spacing if cue_spacing is None else cue_spacing
    # Half the inter-cue gap belongs to each block. Including that padding in
    # extents keeps automatic context, anchoring and transition scroll aligned.
    padding = (cue_spacing - line_spacing) / (2 * height)
    positions = [padding]
    for index in range(1, len(origins)):
        previous, current = origins[index - 1], origins[index]
        step = continuation_step if current == previous else 1.0
        if original_layout[current][0] != original_layout[previous][0]:
            step = 1 + 2 * padding
        elif (current != previous
                and current < len(style_indices) and previous < len(style_indices)
                and style_indices[previous] >= 0 and style_indices[current] < 0):
            step = (height - max(0.0, line_spacing) * (1 - _TRANSLATION_GAP_RATIO)) / height
        positions.append(positions[-1] + step)
    starts, ends = {}, {}
    for origin, position in zip(origins, positions):
        slot = original_layout[origin][0]
        starts.setdefault(slot, position - padding)
        ends[slot] = position + 1.0 + padding
    layout = tuple((original_layout[origin][0], position - starts[original_layout[origin][0]],
                    ends[original_layout[origin][0]] - starts[original_layout[origin][0]])
                   for origin, position in zip(origins, positions))
    centers = {slot: (starts[slot] + end) / 2 for slot, end in ends.items()}
    return layout, centers, positions[-1] + 1.0 + padding


def lyric_cue_extent(item, cue) -> float:
    """Measure a cue in the same row units used by the scrolling strip."""
    from app.services.lyrics_service import LyricsService

    lines = lyric_display_lines(LyricsService.decode_line_breaks(cue.get("text", "")))
    positions = cue.get("display_line_positions", tuple(range(len(lines))))
    source = item.source
    cue_spacing = source.subtitle_line_spacing if source.subtitle_flow_direction in {"left", "right"} else source.subtitle_cue_spacing
    if (not source.subtitle_auto_wrap and not any(index < 0 for index in positions)
            and cue_spacing == source.subtitle_line_spacing):
        return float(max(1, len(lines)))
    height = item._lyric_line_height()
    origins = tuple(index for index, line in enumerate(lines)
                    for _part in lyric_wrapped_rows(item, line, positions[index]))
    layout = tuple((0, index, len(lines)) for index in range(len(lines)))
    return _wrapped_row_layout(layout, origins, height, source.subtitle_line_spacing, tuple(positions), cue_spacing)[2]


def paint_lines(item: "SourceItem", painter: QPainter, rect: QRectF) -> None:
    # Text-only media cannot carry a clickable logo. Keep the documented
    # Papago text attribution visible with its translated result in both
    # preview and export; interactive review screens also provide the link.
    attribution = -2 in item._subtitle_line_style_indices
    content = rect.adjusted(0, 0, 0, -20) if attribution and rect.height() > 40 else rect
    _paint_lines(item, painter, content)
    if attribution:
        painter.save()
        painter.setClipRect(rect, Qt.ClipOperation.IntersectClip)
        font = QFont()
        font.setPixelSize(11)
        painter.setFont(font)
        painter.setPen(QColor("#B8C4D4"))
        painter.drawText(QRectF(rect.left() + 8, rect.bottom() - 18, max(0, rect.width() - 16), 16),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, "Translated by Papago")
        painter.restore()


def _paint_lines(item: "SourceItem", painter: QPainter, rect: QRectF) -> None:
    source = item.source
    painter.save()
    painter.setClipRect(rect, Qt.ClipOperation.IntersectClip)
    lines = lyric_display_lines(item._render_text() or source.subtitle_fallback)
    current = (source.subtitle_current_line if item._music_preview_current_line is None
               else item._music_preview_current_line)
    count = max(1, source.subtitle_current_line_count)
    has_current = 0 <= current < len(lines)
    anchor = item._subtitle_anchor_line
    original_anchor = anchor
    anchored = 0 <= anchor < len(lines)
    anchor_count = max(1, item._subtitle_anchor_line_count)
    boundary = current if has_current else (anchor if anchored else -1)
    height = item._lyric_line_height()
    t = max(0.0, min(1.0, item._subtitle_transition_progress))
    transitioning = t < 1.0 and source.subtitle_animation != "none"
    effect = source.subtitle_animation
    layout = item._subtitle_cue_layout
    if len(layout) != len(lines):
        layout = tuple((index, 0, 1) for index in range(len(lines)))
    anchor_slot = layout[anchor][0] if anchored else (layout[current][0] if has_current else 0)
    centers = {slot: index - inner + total / 2 for index, (slot, inner, total) in enumerate(layout)}
    last_slot = layout[-1][0] if layout else 0
    horizontal = source.subtitle_flow_direction in {"left", "right"}
    strip_window = item._subtitle_strip_window
    if strip_window is not None:
        last_slot = strip_window[3] - 1
    sign = -1.0 if source.subtitle_flow_direction in {"down", "right"} else 1.0
    slots = max(1, (source.subtitle_context_lines if source.subtitle_context_lines >= 0 else 1)
                + (source.subtitle_next_lines if source.subtitle_next_lines >= 0 else 1) + 1)
    step = rect.width() / slots if horizontal else height
    width = max(16.0, step - source.subtitle_cue_spacing) if horizontal else max(16.0, rect.width() - 24)
    # Expand display rows without changing the cue data or its original indices.
    origins = list(range(len(lines)))
    offsets = [0] * len(lines)
    original_layout = layout
    original_style_indices = item._subtitle_line_style_indices
    compact_layout = (source.subtitle_auto_wrap or any(index < 0 for index in original_style_indices)
        or not horizontal and source.subtitle_cue_spacing != source.subtitle_line_spacing)
    block_extent = float(len(lines))
    if compact_layout and lines:
        expanded, origins, offsets, boundaries = [], [], [], [0]
        for index, line in enumerate(lines):
            style_index = original_style_indices[index] if index < len(original_style_indices) else index
            for part, offset in lyric_wrapped_rows(item, line, style_index):
                expanded.append(part)
                origins.append(index)
                offsets.append(offset)
            boundaries.append(len(expanded))
        wrapped_layout, centers, block_extent = _wrapped_row_layout(
            original_layout, tuple(origins), height, source.subtitle_line_spacing, original_style_indices,
            source.subtitle_line_spacing if horizontal else source.subtitle_cue_spacing)
        if has_current:
            count = boundaries[min(len(lines), current + count)] - boundaries[current]
            current = boundaries[current]
        if anchored:
            anchor_count = boundaries[min(len(lines), anchor + anchor_count)] - boundaries[anchor]
            anchor = boundaries[anchor]
        boundary = current if has_current else (anchor if anchored else -1)
        lines, layout = expanded, tuple(wrapped_layout)
    else:
        boundaries = list(range(len(lines) + 1))
    # The active group holds all simultaneous phrases, but the newest one
    # should occupy the normal current cue position, not the group's midpoint.
    prior_rows = max(0, item._subtitle_overlap_prior_rows) if anchored and has_current else 0
    if prior_rows:
        first = max(0, original_anchor)
        prior_rows = boundaries[min(len(boundaries) - 1, first + prior_rows)] - boundaries[first]
    overlap_shift = prior_rows * height / 2
    axis = rect.width() if horizontal else rect.height()
    anchor_extent = layout[anchor][2] if anchored else (
        layout[current][2] if compact_layout and has_current else anchor_count)
    def anchor_at(extent):
        guard = min(axis / 2, step / 2 if horizontal else height * extent / 2)
        return max(guard, min(axis - guard, axis * source.subtitle_anchor))

    anchor_position = anchor_at(anchor_extent)
    if not horizontal:
        anchor_position -= overlap_shift
    alignment = _ALIGNMENTS.get(source.text_alignment, Qt.AlignmentFlag.AlignHCenter)
    flags = alignment | Qt.AlignmentFlag.AlignVCenter | Qt.TextFlag.TextSingleLine
    pixel_ratio = device_pixel_ratio(painter)
    base_opacity = painter.opacity()
    previous_count = item._subtitle_previous_line_count if transitioning else 0
    leaving = item._subtitle_leaving_line_count if transitioning else 0
    visible_end = len(original_layout) - item._subtitle_prefetch_line_count
    prepared_end = visible_end + item._subtitle_prefetch_entry_line_count
    entering = visible_end - item._subtitle_entering_line_count if transitioning else visible_end
    if compact_layout:
        old_boundary = origins[boundary] if boundary >= 0 else -1
        if previous_count and old_boundary >= 0:
            previous_count = boundaries[old_boundary] - boundaries[max(0, old_boundary - previous_count)]
        leaving = boundaries[min(leaving, len(boundaries) - 1)]
        entering = boundaries[max(0, entering)]
        visible_end = boundaries[visible_end]
        prepared_end = boundaries[prepared_end]
    if visible_end < len(lines):
        last_slot = layout[visible_end - 1][0]
        block_extent = centers[last_slot] + layout[visible_end - 1][2] / 2
    intro = item._subtitle_intro_state
    previous_extent = layout[boundary - 1][2] if previous_count and boundary > 0 else previous_count
    if strip_window is not None and previous_count:
        # A tall wrapped cue can push the anchor away from an edge. That
        # adjustment belongs to the shared scroll, including its first frame.
        anchor_position = anchor_at(previous_extent) * (1 - t) + anchor_position * t
    strip_scroll = (previous_extent + anchor_extent) / 2 * (1 - t) if previous_count else 0.0
    current_extent = layout[current][2] if has_current else count
    untimed = not item._subtitle_cue_layout and not has_current and intro is None
    intro_y = rect.center().y() if horizontal else rect.top() + anchor_position
    held_slot = intro.held_slot if intro is not None else anchor_slot
    held_rows = [index for index, (slot, _inner, _total) in enumerate(layout) if slot == held_slot]
    held_end = centers[held_slot] + layout[held_rows[0]][2] / 2 if held_rows else 0
    if strip_window is not None and intro is None:
        old_first, old_last, new_first, new_last = strip_window
        extents = {slot: total for slot, _inner, total in layout}

        def window_edges(first, last, current_slot, position):
            first_y = (rect.top() + position + sign * (centers[first] - centers[current_slot]) * height
                       - sign * extents[first] * height / 2)
            last_y = (rect.top() + position + sign * (centers[last - 1] - centers[current_slot]) * height
                      + sign * extents[last - 1] * height / 2)
            return (first_y, last_y) if sign > 0 else (last_y, first_y)

        old_slot = anchor_slot - 1 if previous_count else anchor_slot
        old_edges = window_edges(old_first, old_last, old_slot,
                                 anchor_at(previous_extent if previous_count else anchor_extent))
        new_edges = window_edges(new_first, new_last, anchor_slot, anchor_at(anchor_extent))
        top, bottom = (a * (1 - t) + b * t for a, b in zip(old_edges, new_edges))
        top_setting, bottom_setting = (source.subtitle_context_lines, source.subtitle_next_lines)
        if sign < 0:
            top_setting, bottom_setting = bottom_setting, top_setting
        # Explicit context counts define a smaller viewport. Cues pass under
        # its edges in strip coordinates instead of fading into an empty slot.
        top = max(rect.top(), top) if top_setting >= 0 else rect.top()
        bottom = min(rect.bottom(), bottom) if bottom_setting >= 0 else rect.bottom()
        painter.setClipRect(QRectF(rect.left(), top, rect.width(), max(0.0, bottom - top)),
                            Qt.ClipOperation.IntersectClip)
    if intro is not None:
        # The indicator is a temporary cue: enter/leave on the same clock and
        # direction as the surrounding lyrics. Its inner dot cycle is independent.
        painter.save()
        travel, scale, _glow, _reveal, opacity = cue_effect_pose(
            source, intro.progress, intro.opacity, current=not intro.exiting,
            previous=intro.exiting, incoming_visible=False, transitioning=intro.raw_progress < 1,
        )
        if intro.exiting:
            # The marker has its own monotone exit; do not replay the lyric's
            # pulse, bounce or zoom underneath it.
            travel, scale, opacity = 0.0, 1.0, 1.0
        distance = step if horizontal else height
        next_distance = (current_extent + 1) / 2 if not horizontal and has_current and anchor_slot > held_slot else 1.0
        departure = -distance * next_distance * intro.progress if intro.exiting else (
            0.0 if intro.before_first else distance * (1 - intro.progress)
            * (1 if horizontal else ((layout[held_rows[0]][2] if held_rows else 0) + 1) / 2)
        )
        painter.translate(sign * (departure + travel) if horizontal else 0,
                          sign * (departure + travel) if not horizontal else 0)
        intro_rect = QRectF(rect.left() + anchor_position - width / 2, rect.top(), width, rect.height()) if horizontal else rect
        painter.translate(intro_rect.center().x(), intro_y)
        painter.scale(scale, scale)
        painter.translate(-intro_rect.center().x(), -intro_y)
        painter.setOpacity(base_opacity * opacity)
        paint_intro(item, painter, intro_rect, intro_y)
        painter.restore()

    recorder = getattr(item, "_export_text_quads", None)
    for index, line in enumerate(lines):
        slot, inner, total = layout[index]
        p = stagger_progress(source, item._subtitle_transition_raw, slot, anchor_slot + 1, last_slot) if (
            transitioning and effect == "cascade" and slot > anchor_slot
        ) else t
        original_index = origins[index]
        line_index = original_style_indices[original_index] if original_index < len(original_style_indices) else original_index
        overrides = lyric_line_overrides(source, line_index)
        karaoke_line = line_index >= 0 and (not source.subtitle_karaoke_lines or line_index in source.subtitle_karaoke_lines)
        if untimed:
            # A fallback message has no cue role. Keep subtitle-role blur,
            # opacity and typography from treating it as an upcoming lyric.
            previous = active = upcoming = {
                "color": source.outline_color, "size": source.font_size,
                "font_weight": source.font_weight, "italic": source.text_italic,
                "scale": 1.0, "opacity": 1.0, "blur": 0.0,
            }
        else:
            previous = _display_line_style(source, "previous", overrides, line_index, karaoke_line)
            active = _display_line_style(source, "current", overrides, line_index, karaoke_line)
            upcoming = _display_line_style(source, "next", overrides, line_index, karaoke_line)
        if slot < anchor_slot:
            span = source.subtitle_context_lines if source.subtitle_context_lines >= 0 else anchor_slot - bool(leaving)
            distance = anchor_slot - slot - (1.0 - t if previous_count else 0.0)
            if strip_window is not None and source.subtitle_context_lines < 0:
                span = (anchor_position if sign > 0 else axis - anchor_position) / height
                distance = centers[anchor_slot] - centers[slot] - strip_scroll
            previous = context_style_at_distance(previous, active, distance, span, source.subtitle_previous_distance_fade)
        if slot > anchor_slot or (slot == anchor_slot and previous_count):
            span = source.subtitle_next_lines if source.subtitle_next_lines >= 0 else last_slot - anchor_slot
            distance = slot - anchor_slot + (1.0 - p if previous_count else 0.0)
            if strip_window is not None and source.subtitle_next_lines < 0:
                span = (axis - anchor_position if sign > 0 else anchor_position) / height
                distance = centers[slot] - centers[anchor_slot] + strip_scroll
            upcoming = context_style_at_distance(upcoming, active, distance, span, source.subtitle_next_distance_fade)
        is_current = has_current and current <= index < current + count
        original_inner = original_layout[original_index][1]
        karaoke_current = (is_current and source.subtitle_karaoke_enabled and karaoke_line
                           and original_inner < len(item._subtitle_word_reveal))
        karaoke_alpha = float(active["opacity"]) * item._subtitle_emphasis if karaoke_current else 0.0
        was_current = 0 <= boundary - previous_count <= index < boundary
        if is_current:
            start, end = (upcoming, active) if transitioning else (previous, active)
            blend = p if transitioning else item._subtitle_emphasis
        elif was_current:
            start, end = previous, active
            blend = item._subtitle_previous_emphasis * (1.0 - p)
        else:
            # A retained anchor has finished singing. It is history, even
            # though its row remains centered until a successor appears.
            held = anchored and not has_current and not item._subtitle_before_first and slot == anchor_slot
            start = end = previous if index < boundary or held else upcoming
            blend = 0.0
        color = _mix(QColor(str(start["color"])), QColor(str(end["color"])), blend)
        size = float(start["size"]) + (float(end["size"]) - float(start["size"])) * blend
        alpha = float(start["opacity"]) + (float(end["opacity"]) - float(start["opacity"])) * blend
        blur = float(start["blur"]) + (float(end["blur"]) - float(start["blur"])) * blend
        weight = round(float(start["font_weight"]) + (float(end["font_weight"]) - float(start["font_weight"])) * blend)
        italic = bool((end if blend >= 0.5 else start)["italic"])
        # Gap readability is the underlying lyric state. Blend the temporary
        # row's previous/next styling AFTER it, so a held cue does not stay
        # sharp while the marker occupies the current position.
        if item._subtitle_gap_unblurred and slot == anchor_slot:
            blur = 0.0
        if (original_index in item._subtitle_blur_restore_lines
                and (intro is None or not intro.exiting)):
            # During an interlude the held cue has already acquired its
            # previous-role blur. Replaying the gap restoration would dip
            # that blur again halfway through the marker's exit.
            blur *= item._subtitle_blur_restore_progress
        waiting_amount = 0.0
        if intro is not None:
            role = "next" if intro.before_first or slot > held_slot else "previous"
            distance = slot + 1 if intro.before_first else (
                slot - held_slot if role == "next" else held_slot - slot + 1
            )
            if intro.before_first:
                span = source.subtitle_next_lines + 1 if source.subtitle_next_lines >= 0 else last_slot + 1
            elif role == "next":
                span = source.subtitle_next_lines if source.subtitle_next_lines >= 0 else last_slot - held_slot
            else:
                span = source.subtitle_context_lines + 1 if source.subtitle_context_lines >= 0 else held_slot + 1
            waiting = context_style_at_distance(
                _display_line_style(source, role, overrides, line_index, karaoke_line), active, distance, span,
                source.subtitle_next_distance_fade if role == "next" else source.subtitle_previous_distance_fade,
            )
            amount = intro.opacity
            if effect == "cascade" and role == "next" and not is_current:
                delayed = stagger_progress(source, intro.raw_progress, slot, max(0, held_slot + 1), last_slot)
                amount = 1 - delayed if intro.exiting else delayed
            waiting_amount = amount
            color = _mix(color, QColor(str(waiting["color"])), amount)
            size += (float(waiting["size"]) - size) * amount
            alpha += (float(waiting["opacity"]) - alpha) * amount
            blur += (float(waiting["blur"]) - blur) * amount
            weight = round(weight + (float(waiting["font_weight"]) - weight) * amount)
            if amount >= 0.5:
                italic = bool(waiting["italic"])
        if is_current and transitioning and not item._subtitle_incoming_visible:
            alpha *= p
        if index < leaving:
            alpha *= 1.0 - p
        offset, extra_scale, glow, reveal, opacity = cue_effect_pose(
            source, p, blend, current=is_current, previous=was_current,
            incoming_visible=item._subtitle_incoming_visible, transitioning=transitioning,
        )
        if intro is None or not intro.exiting:
            offset *= 1 - waiting_amount
        extra_scale = 1 + (extra_scale - 1) * (1 - waiting_amount)
        glow *= 1 - waiting_amount
        reveal *= 1 - waiting_amount
        alpha *= 1 + (opacity - 1) * (1 - waiting_amount)
        if strip_window is not None:
            old_first, old_last, new_first, new_last = strip_window
            old_visible = old_first <= slot < old_last
            new_visible = new_first <= slot < new_last
            if not old_visible and not new_visible:
                continue
        if max(alpha, karaoke_alpha) <= _VISIBLE:
            continue

        scroll = source.subtitle_scroll_offset
        if compact_layout and transitioning and not horizontal:
            scroll = (previous_extent + anchor_extent) / 2 * height * (1 - t) if previous_count else 0.0
        if transitioning and effect == "cascade":
            scroll = (previous_extent + anchor_extent) / 2 * height * (1.0 - p) if previous_count else 0.0
        if horizontal:
            scroll = (step * (1.0 - p) if previous_count else 0.0) if transitioning else 0.0
        if intro is not None and intro.exiting:
            # The virtual cue already moved the held lyric aside. Interpolate
            # once toward the new layout, rather than applying two scrolls.
            scroll = 0.0
        layout_offset = 0.0 if intro is not None else offset
        if horizontal:
            x = rect.left() + anchor_position + sign * ((slot - anchor_slot) * step + scroll + layout_offset)
            row = QRectF(x - width / 2, rect.center().y() + (inner - total / 2) * height - overlap_shift, width, height)
        elif anchored:
            distance = (centers[slot] - centers[anchor_slot]) * height
            row = QRectF(rect.left() + 12, rect.top() + anchor_position + sign * (
                distance + scroll + layout_offset
            ) + (inner - total / 2) * height, width, height)
        else:
            row = QRectF(rect.left() + 12, rect.top() + anchor_position + sign * (
                centers[slot] - block_extent / 2
            ) * height + (inner - total / 2) * height + sign * layout_offset, width, height)
        if intro is not None:
            if horizontal:
                relative = slot + 1 if intro.before_first else slot - held_slot - (1 if slot <= held_slot else 0)
                waiting_x = rect.left() + anchor_position + sign * relative * step
                row.translate((waiting_x - row.center().x()) * waiting_amount, 0)
            else:
                relative = centers[slot] + (1 if intro.before_first or slot > held_slot else 0) - held_end - 0.5
                waiting_y = intro_y + sign * relative * height + (inner - (total - 1) / 2) * height
                row.translate(0, (waiting_y - row.center().y()) * waiting_amount)
            row.translate(sign * offset if horizontal else 0, sign * offset if not horizontal else 0)
            if horizontal:
                x = row.center().x()

        if strip_window is not None:
            # Replacing an offscreen prefix must not change Qt's subpixel
            # raster origin through floating-point cancellation.
            row.moveTop(round(row.top(), 6))

        # A stable glyph size lets the role scale interpolate without rebuilding
        # the same blur raster for every intermediate font size.
        font_size = max(float(previous["size"]), float(active["size"]), float(upcoming["size"]))
        font = item._lyric_font(max(8, round(font_size)), weight, italic,
                                str(overrides.get("font_family", "")))
        painter.setFont(font)
        advance = item._lyric_text_advance(font, line) + 2
        fit = min(1.0, width / max(1.0, advance * size / font_size)) if source.subtitle_auto_shrink else 1.0
        fit = max(0.01 if source.subtitle_auto_wrap else (0.1 if horizontal else _MIN_FIT), fit)
        glyph_width = max(width, advance)
        row.setWidth(glyph_width)
        if alignment == Qt.AlignmentFlag.AlignRight:
            row.moveLeft((x + width / 2 if horizontal else rect.right() - 12) - glyph_width)
            pivot_x = x + width / 2 if horizontal else rect.right() - 12
        elif alignment == Qt.AlignmentFlag.AlignLeft:
            pivot_x = row.left()
        else:
            pivot_x = x if horizontal else rect.center().x()
            row.moveLeft(pivot_x - glyph_width / 2)
        painter.save()
        if index >= entering and strip_window is None:
            # New upcoming cues travel through the clipped edge at their normal
            # opacity, rather than materializing in the empty tail area. Every
            # physical line in a cue shares the same translation.
            padding = max(0.0, float(role_style(source, "next")["blur"])) * 3 + 2
            # Prefetched rows wait beyond the edge using the same entry origin
            # as a newly displayed cue, without adding any scroll distance.
            prefetch = index >= visible_end
            entry_index = visible_end if prefetch else entering
            # Advance only cues that will be visible in the following window;
            # other staged cues remain outside, ready for later boundaries.
            entry_progress = (item._subtitle_prefetch_progress if prefetch and index < prepared_end
                              else 0.0 if prefetch else p)
            entry_slot, _entry_inner, entry_total = layout[entry_index]
            if horizontal:
                entry_x = x + sign * (entry_slot - slot) * step
                distance = (rect.right() - (entry_x - width / 2) + padding if sign > 0 else
                            entry_x + width / 2 - rect.left() + padding)
                painter.translate(sign * max(0.0, distance) * (1 - entry_progress), 0)
            else:
                entry_center = (row.center().y() - (inner - (total - 1) / 2) * height
                                + sign * (centers[entry_slot] - centers[slot]) * height)
                distance = (rect.bottom() - (entry_center - entry_total * height / 2) + padding
                            if sign > 0 else entry_center + entry_total * height / 2 - rect.top() + padding)
                painter.translate(0, sign * max(0.0, distance) * (1 - entry_progress))
        if is_current and source.uses_bass_reaction:
            pose = music_reactive_pose(source.music_reactive_effect, item._music_reaction_level,
                                       source.music_reactive_strength, float(active["size"]))
            # Every physical line of the current cue shares one pivot.
            center = QPointF(pivot_x, row.center().y() + ((total - 1) / 2 - inner) * height)
            painter.translate(pose.dx, pose.dy)
            painter.translate(center)
            painter.rotate(pose.rotation)
            painter.scale(pose.scale * pose.scale_x, pose.scale)
            painter.translate(-center)
        scale = size / font_size * extra_scale * fit
        pivot = QPointF(pivot_x, row.center().y())
        painter.translate(pivot)
        painter.scale(scale, scale)
        painter.translate(-pivot)
        for radius, opacity in (
            (source.subtitle_glow_radius, alpha * (reveal + glow)),
            (blur, alpha * (1.0 - reveal)),
        ):
            if opacity > _VISIBLE and radius > 0:
                pixmap, margin = item._lyric_blur_pixmap(line, color, radius, glyph_width, height, pixel_ratio, flags, font)
                painter.setOpacity(base_opacity * min(1.0, opacity))
                origin = QPointF(row.left() - margin, row.top() - margin)
                if recorder is None:
                    painter.drawPixmap(origin, pixmap)
                else:
                    recorder.pixmap(painter, origin, pixmap)
        sharp = alpha * (1.0 - reveal) if blur <= 0.0 else 0.0
        if sharp > _VISIBLE:
            painter.setOpacity(base_opacity * sharp)
            painter.setPen(color)
            item._draw_text(painter, row, flags, line)
        # Sung words follow the audio clock even while their row fades or unblurs.
        if karaoke_alpha > _VISIBLE:
            painter.setOpacity(base_opacity * karaoke_alpha)
            revealed = max(0.0, item._subtitle_word_reveal[original_inner] - offsets[index])
            highlights = (item._subtitle_word_highlight_ranges[original_inner]
                          if original_inner < len(item._subtitle_word_highlight_ranges) else None)
            if highlights is None:
                paint_word_reveal(item, painter, row, flags, line, revealed)
            else:
                # A wrapped row covers only part of its original lyric line.
                # Intersect every independently active word with that slice.
                local_ranges = tuple(
                    (max(0.0, a - offsets[index]), min(float(len(line)), b - offsets[index]))
                    for a, b in highlights
                    if b > offsets[index] and a < offsets[index] + len(line)
                )
                paint_word_reveal(item, painter, row, flags, line, revealed,
                                  highlight_ranges=local_ranges)
        painter.restore()
    painter.restore()


def _qtextline_cursor_x(line, cursor: int) -> float:
    """Handle scalar and (x, adjusted-cursor) Qt/PySide6 return variants.

    The track-details TTML widget supports both forms already; the shared
    Canvas/MP4 karaoke renderer must not crash on the scalar-only binding.
    """
    result = line.cursorToX(cursor)
    return float(result[0] if isinstance(result, tuple) else result)


def paint_word_reveal(item, painter: QPainter, row: QRectF, flags, text: str, revealed: float,
                      *, highlight_ranges: tuple[tuple[float, float], ...] | None = None) -> None:
    """Reuse shaped glyphs while supporting multiple simultaneously sung words."""
    if revealed <= 0 and not highlight_ranges:
        return
    key = (painter.font().key(), text, row.width(), row.height(), str(flags), item._line_spacing(flags))
    cached = item._lyric_reveal_cache.get(key)
    if cached is None:
        if len(item._lyric_reveal_cache) >= 64:
            item._lyric_reveal_cache.clear()
        layout = QTextLayout(text, painter.font())
        option = QTextOption()
        option.setAlignment(flags & (Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignRight
                                     | Qt.AlignmentFlag.AlignHCenter))
        option.setWrapMode(QTextOption.WrapMode.NoWrap)
        layout.setTextOption(option)
        layout.beginLayout()
        line = layout.createLine()
        line.setLineWidth(row.width())
        layout.endLayout()
        positions, cursor = [_qtextline_cursor_x(line, 0)], 0
        for char in text:
            cursor += 2 if ord(char) > 0xFFFF else 1
            positions.append(_qtextline_cursor_x(line, cursor))
        path = item._text_layout_path(painter, QRectF(0, 0, row.width(), row.height()), flags, text)
        cached = (tuple(positions), path)
        item._lyric_reveal_cache[key] = cached
    positions, path = cached
    accent = QColor(item.source.subtitle_accent_color)
    recorder = getattr(item, "_export_text_quads", None)

    def draw_accent():
        if recorder is None:
            painter.fillPath(path, accent)
        else:
            recorder.accent(painter, path, accent, key)

    def cursor_x(position: float) -> float:
        position = max(0.0, min(float(len(text)), position))
        whole = int(position)
        x = positions[whole]
        return x + (positions[whole + 1] - x) * (position - whole) if whole < len(text) else x

    painter.save()
    painter.translate(row.topLeft())
    if highlight_ranges is None:
        # Preserve the established prefix renderer exactly for ordinary lyrics,
        # including bidi text whose cursor endpoints do not bound the glyphs.
        whole = min(len(text), int(revealed))
        left, boundary = positions[0], positions[whole]
        if whole < len(text):
            boundary += (positions[whole + 1] - boundary) * (revealed - whole)
            painter.setClipRect(QRectF(min(left, boundary), 0, abs(boundary - left), row.height()),
                                Qt.ClipOperation.IntersectClip)
        draw_accent()
    else:
        for start, end in highlight_ranges:
            if end <= start:
                continue
            left, right = cursor_x(start), cursor_x(end)
            painter.save()
            painter.setClipRect(QRectF(min(left, right), 0, abs(right - left), row.height()),
                                Qt.ClipOperation.IntersectClip)
            draw_accent()
            painter.restore()
    painter.restore()


def paint_intro(item: "SourceItem", painter: QPainter, rect: QRectF, y: float) -> None:
    """Small vector indicators: brightness, breathing, travel, height and rotation."""
    source, state = item.source, item._subtitle_intro_state
    style = role_style(source, "current")
    unit = max(4.0, source.font_size * float(style["scale"]) * source.subtitle_intro_scale)
    radius = min(unit * 0.095, rect.width() / 18, rect.height() / 8)
    spacing = radius * 4.8
    kind = source.subtitle_intro_style
    count = 5 if kind == "bars" else 3
    width = radius * 8 if kind in {"ring", "orbit", "ripple"} else (count - 1) * spacing + radius * 2
    x = rect.center().x()
    x = max(rect.left() + width / 2 + 12, min(rect.right() - width / 2 - 12, x))
    y = max(rect.top() + radius * 3, min(rect.bottom() - radius * 3, y))
    if source.text_alignment != "center":
        x = rect.left() + width / 2 + 12 if source.text_alignment == "left" else rect.right() - width / 2 - 12
    # Hold the last waiting pose during departure, preventing a new pulse
    # from brightening a dot that is already fading away.
    phase_time = state.elapsed - max(0.0, -state.remaining) if state.exiting else state.elapsed
    phase = (phase_time / source.subtitle_intro_period) % 1.0
    color = QColor(str(style["color"]))
    exit_pose = intro_exit_pose(source.subtitle_intro_exit, state.raw_progress) if state.exiting else None
    base = painter.opacity() * float(style["opacity"]) * (exit_pose.opacity if exit_pose else state.opacity)
    if exit_pose:
        horizontal = source.subtitle_flow_direction in {"left", "right"}
        sign = -1 if source.subtitle_flow_direction in {"down", "right"} else 1
        painter.translate(sign * exit_pose.travel * unit if horizontal else 0,
                          sign * exit_pose.travel * unit if not horizontal else 0)
        painter.translate(x, y)
        painter.scale(exit_pose.scale * exit_pose.spread, exit_pose.scale * exit_pose.spread)
        painter.translate(-x, -y)
        if exit_pose.reveal < 1:
            painter.setClipRect(QRectF(x - width / 2, y - radius * 4,
                                      width * exit_pose.reveal, radius * 8), Qt.ClipOperation.IntersectClip)
    painter.setPen(Qt.PenStyle.NoPen)
    if kind == "orbit":
        painter.setBrush(color)
        for index in range(3):
            angle = 2 * pi * (phase + index / 3)
            painter.setOpacity(base * (1 - index * .22))
            painter.drawEllipse(QPointF(x + cos(angle) * radius * 2.8, y + sin(angle) * radius * 2.8),
                                radius * .7, radius * .7)
        return
    if kind == "ripple":
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(color, max(1.0, radius * .5)))
        for index in range(3):
            pulse = (phase + index / 3) % 1
            painter.setOpacity(base * sin(pi * pulse) ** 2)
            size = radius * (.4 + 3.1 * pulse)
            painter.drawEllipse(QPointF(x, y), size, size)
        return
    if kind == "comet":
        painter.setBrush(color)
        for index in range(5):
            pulse = (phase - index * .07) % 1
            painter.setOpacity(base * (1 - index / 6) * sin(pi * pulse) ** 2)
            size = radius * (1 - index * .12)
            painter.drawEllipse(QPointF(x + (pulse - .5) * spacing * 2, y), size, size)
        return
    if kind == "ring":
        ring = QRectF(x - radius * 3, y - radius * 3, radius * 6, radius * 6)
        pen = QPen(color, max(1.0, radius * 0.7), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setOpacity(base * 0.18)
        painter.drawEllipse(ring)
        painter.setOpacity(base)
        painter.drawArc(ring, round((90 - phase * 360) * 16), -100 * 16)
        return
    from app.animation.curves import ease_in_out_cubic
    for index in range(count):
        pulse = (1 + sin(2 * pi * (phase - index * 0.13))) / 2
        brightness, shift, scale = 0.25 + 0.75 * pulse, 0.0, 1.0
        if kind == "dots":
            light = ease_in_out_cubic((phase - 0.1 - index * 0.17) / 0.18)
            light *= 1 - ease_in_out_cubic((phase - 0.8) / 0.2)
            brightness = 0.18 + 0.82 * light
        elif kind == "breathing":
            pulse = (1 - sin(2 * pi * phase + pi / 2)) / 2
            brightness, scale = 0.2 + 0.8 * pulse, 0.85 + 0.15 * pulse
        elif kind == "wave":
            shift = -radius * 1.8 * pulse
        painter.setOpacity(base * brightness)
        painter.setBrush(color)
        center = QPointF(x + (index - (count - 1) / 2) * spacing, y + shift)
        if kind == "bars":
            height = radius * (2 + 4 * pulse)
            painter.drawRoundedRect(QRectF(center.x() - radius * 0.55, y - height / 2,
                                          radius * 1.1, height), radius * 0.55, radius * 0.55)
        else:
            painter.drawEllipse(center, radius * scale, radius * scale)
