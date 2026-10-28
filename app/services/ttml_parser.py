"""TTML lyric timing, including Apple Music's document-relative lyric profile.

Timing follows https://www.w3.org/TR/ttml2/#timing and Apple's TTML lyrics
profile. Presentation styles remain controlled by the application's sources.
"""

from __future__ import annotations

from math import inf, isfinite
import re
from xml.etree import ElementTree as ET


class TTMLParseError(ValueError):
    pass


_PARAM = "{http://www.w3.org/ns/ttml#parameter}"
_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
_APPLE = re.compile(r'xmlns(?::[\w.-]+)?\s*=\s*[\"\']https?://(?:itunes\.apple\.com/lyric-ttml-extensions|music\.apple\.com/lyric-ttml-internal)[\"\']')


def parse_ttml(content: str) -> list[dict[str, object]]:
    """Read line and inline timings without generating timing from text/audio."""
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", content, re.I):
        raise TTMLParseError("TTML document type declarations and entities are not supported.")
    try:
        root = ET.fromstring(content)
    except (ET.ParseError, ValueError) as error:
        raise TTMLParseError(f"Invalid TTML XML: {error}") from error

    def tag(node):
        return node.tag.rsplit("}", 1)[-1]

    if tag(root) != "tt" or root.tag not in {"tt", "{http://www.w3.org/ns/ttml}tt", "{http://www.w3.org/2006/10/ttaf1}tt"}:
        raise TTMLParseError("Expected a TTML <tt> document.")
    if root.get(_PARAM + "timeBase", "media") != "media":
        raise TTMLParseError("Only media-time TTML lyrics are supported.")
    apple = bool(_APPLE.search(content))
    line_timing = any(key.endswith("}timing") and value.lower() == "line" for key, value in root.attrib.items())
    try:
        frame_rate = int(root.get(_PARAM + "frameRate", "30"))
        subframes = int(root.get(_PARAM + "subFrameRate", "1"))
        multiplier = root.get(_PARAM + "frameRateMultiplier", "1 1").split()
        numerator, denominator = map(int, multiplier)
        effective_rate = frame_rate * numerator / denominator
        tick_rate = float(root.get(_PARAM + "tickRate", str(effective_rate * subframes if _PARAM + "frameRate" in root.attrib else 1)))
        if min(frame_rate, subframes, numerator, denominator, tick_rate) <= 0 or not isfinite(tick_rate):
            raise ValueError()
    except (ValueError, ZeroDivisionError, OverflowError) as error:
        raise TTMLParseError("Invalid TTML frame or tick rate.") from error

    def seconds(value):
        value = value.strip()
        # Apple lyric files also use M:SS.mmm (for example 2:02.987),
        # whereas standard TTML clock components are zero-padded.
        minutes_pattern = r"\d+" if apple else r"\d{2}"
        clock = re.fullmatch(r"(?:(\d+):)?(" + minutes_pattern + r"):(\d{2})(?:\.(\d+)|:(\d{2})(?:\.(\d+))?)?", value)
        if clock:
            hours, minutes, secs, fraction, frames, sub = clock.groups()
            if ((hours is not None and int(minutes) >= 60) or (not apple and int(minutes) >= 60)
                    or int(secs) >= 60 or (frames is not None and int(frames) >= frame_rate)):
                raise TTMLParseError(f"Invalid TTML time: {value}")
            if sub is not None and int(sub) >= subframes:
                raise TTMLParseError(f"Invalid TTML subframe: {value}")
            result = int(hours or 0) * 3600 + int(minutes) * 60 + int(secs)
            result += float("0." + fraction) if fraction else (int(frames or 0) + int(sub or 0) / subframes) / effective_rate
        else:
            offset = re.fullmatch(r"(\d+(?:\.\d+)?)(ms|h|m|s|f|t)?", value)
            if not offset or (offset[2] is None and not apple):
                raise TTMLParseError(f"Invalid TTML time: {value}")
            result = float(offset[1]) * {"h": 3600, "m": 60, "s": 1, "ms": .001,
                                          "f": 1 / effective_rate, "t": 1 / tick_rate, None: 1}[offset[2]]
        if not isfinite(result):
            raise TTMLParseError("TTML time must be finite.")
        return result

    intervals, spaces = {}, {}
    paragraphs = []

    def visit(node, reference=0.0, parent_end=inf, preserve=False, depth=0):
        if depth > 100:
            raise TTMLParseError("TTML nesting is too deep.")
        preserve = {"preserve": True, "default": False}.get(node.get(_SPACE), preserve)
        spaces[node] = preserve
        base = 0.0 if apple else reference
        start = base + seconds(node.get("begin")) if "begin" in node.attrib else reference
        explicit_end = base + seconds(node.get("end")) if "end" in node.attrib else inf
        duration_end = start + seconds(node.get("dur")) if "dur" in node.attrib else inf
        if min(explicit_end, duration_end) < start:
            raise TTMLParseError("TTML interval ends before it starts.")
        end = min(parent_end, explicit_end, duration_end)
        start = max(reference, start)
        mode = node.get("timeContainer", "par")
        if mode not in {"par", "seq"}:
            raise TTMLParseError("Invalid TTML time container.")
        cursor, child_ends = start, []
        for child in node:
            if tag(child) not in {"body", "div", "p", "span", "br"}:
                continue
            child_end = visit(child, cursor if mode == "seq" else start, end, preserve, depth + 1)
            if tag(child) != "br":
                child_ends.append(child_end)
            if mode == "seq":
                if not isfinite(child_end):
                    raise TTMLParseError("Sequential TTML elements need a finite duration.")
                cursor = child_end
        # Explicit inline times can determine an otherwise unbounded paragraph.
        if not isfinite(end) and child_ends and all(isfinite(e) for e in child_ends):
            end = max(child_ends)
        intervals[node] = (start, end)
        if tag(node) == "p":
            paragraphs.append(node)
        return end

    try:
        visit(root)
        cues = []
        for paragraph in paragraphs:
            start, end = intervals[paragraph]
            if end <= start:
                continue
            chars = []

            def append(text, timing, preserve=False):
                for char in text or "":
                    if not preserve and char in " \t\r\n":
                        char = " "
                        if not chars or chars[-1][0] in " \n":
                            continue
                    chars.append((char, timing))

            def collect(node, timing=None):
                own_timing = not line_timing and tag(node) == "span" and any(key in node.attrib for key in ("begin", "end", "dur"))
                if own_timing:
                    a, b = intervals[node]
                    timing = (max(start, a), min(end, b))
                append(node.text, timing, spaces[node])
                for child in node:
                    if tag(child) == "br":
                        while chars and chars[-1][0] == " ":
                            chars.pop()
                        chars.append(("\n", None))
                    elif tag(child) == "span":
                        role = next((v for k, v in child.attrib.items() if k.rsplit("}", 1)[-1] == "role"), "")
                        if role in {"x-translation", "x-roman", "x-bg"} and chars and chars[-1][0] != "\n":
                            chars.append(("\n", None))
                        collect(child, timing)
                    append(child.tail, timing, spaces[node])

            collect(paragraph)
            while chars and chars[0][0].isspace():
                chars.pop(0)
            while chars and chars[-1][0].isspace():
                chars.pop()
            text = "".join(c for c, _ in chars)
            if not text:
                continue
            if not isfinite(end):
                raise TTMLParseError("TTML lyric lines need an end, duration, or finite inline timings.")
            words, index = [], 0
            while index < len(chars):
                timing = chars[index][1]
                limit = index + 1
                while limit < len(chars) and chars[limit][1] == timing and chars[limit][0] != "\n":
                    limit += 1
                segment = text[index:limit]
                if timing is not None and segment.strip() and timing[1] > timing[0]:
                    words.append({"start": timing[0], "end": timing[1], "text": segment, "offset": index})
                index = limit
            cue = {"start": start, "end": end, "text": text, "format": "ttml"}
            if words:
                cue["words"] = words
            cues.append(cue)
        cues.sort(key=lambda cue: (cue["start"], cue["end"]))
        # Paragraphs with identical intervals (for example original and
        # translation) may share a cue. Different end times must stay separate.
        merged = []
        for cue in cues:
            if merged and (cue["start"], cue["end"]) == (merged[-1]["start"], merged[-1]["end"]):
                previous = merged[-1]
                offset = len(previous["text"]) + 1
                previous["text"] += "\n" + cue["text"]
                previous["end"] = max(previous["end"], cue["end"])
                if cue.get("words"):
                    previous.setdefault("words", []).extend(word | {"offset": word["offset"] + offset} for word in cue["words"])
            else:
                merged.append(cue)
        return merged
    except (ValueError, OverflowError, RecursionError) as error:
        if isinstance(error, TTMLParseError):
            raise
        raise TTMLParseError(f"Invalid TTML timing: {error}") from error
