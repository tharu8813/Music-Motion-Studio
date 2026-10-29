"""Decode KRC timing records to standard TTML without estimating timings."""

import base64
import binascii
import re
import zlib
from xml.etree import ElementTree as ET


class KRCError(ValueError):
    pass


def krc_to_ttml(encoded: str, *, max_bytes: int, check_cancel=lambda: None) -> str:
    if not isinstance(encoded, str) or len(encoded) > max_bytes * 2:
        raise KRCError("Invalid or oversized KRC content.")
    try:
        raw = base64.b64decode(encoded, validate=True)
        if not raw.startswith(b"krc1"):
            raise KRCError("Expected a KRC lyric document.")
        # Fixed mask belonging to the KRC file format, not a user credential.
        mask = bytes((64, 71, 97, 119, 94, 50, 116, 71, 81, 54, 49, 45, 206, 210, 110, 105))
        check_cancel()
        compressed = bytes(value ^ mask[index % 16] for index, value in enumerate(raw[4:]))
        decoder = zlib.decompressobj()
        decoded = decoder.decompress(compressed, max_bytes + 1)
        if len(decoded) > max_bytes:
            raise KRCError("KRC lyric response is too large.")
        if not decoder.eof or decoder.unused_data:
            raise KRCError("Incomplete or invalid KRC compression.")
        content = decoded.decode("utf-8-sig")
    except (binascii.Error, UnicodeError, zlib.error) as error:
        raise KRCError("Cannot decode KRC lyrics.") from error

    root = ET.Element("tt", {"xmlns": "http://www.w3.org/ns/ttml",
                             "{http://www.w3.org/XML/1998/namespace}space": "preserve"})
    body = ET.SubElement(root, "body")
    count = 0
    for line in content.splitlines():
        check_cancel()
        match = re.fullmatch(r"\[(-?\d{1,9}),(-?\d{1,9})\](.*)", line)
        if match is None:
            # KRC metadata ([ti:], [language:], ...) is not lyric content.
            if re.match(r"\[[\d-]", line):
                raise KRCError("Invalid KRC lyric line.")
            continue
        start, duration, text = int(match[1]), int(match[2]), match[3]
        if start < 0 or duration <= 0 or start + duration > 86400000:
            raise KRCError("Invalid KRC line interval.")
        if not text.strip():
            continue
        count += 1
        if count > 20000:
            raise KRCError("Too many KRC lyric lines.")
        paragraph = ET.SubElement(body, "p", {"begin": f"{start}ms", "end": f"{start + duration}ms"})
        markers = list(re.finditer(r"<([^<>]*)>", text))
        if not markers:
            paragraph.text = text
            continue
        paragraph.text = text[:markers[0].start()]
        previous_end = 0
        for index, marker in enumerate(markers):
            check_cancel()
            fields = re.fullmatch(r"(\d{1,9}),(\d{1,9}),(\d{1,9})", marker[1])
            if fields is None:
                raise KRCError("Invalid KRC word timestamp.")
            offset, length = int(fields[1]), int(fields[2])
            if not previous_end <= offset <= offset + length <= duration:
                raise KRCError("Invalid KRC word interval.")
            end = markers[index + 1].start() if index + 1 < len(markers) else len(text)
            segment = text[marker.end():end]
            word = ET.SubElement(paragraph, "span", {"begin": f"{offset}ms", "end": f"{offset + length}ms"})
            word.text = segment
            previous_end = offset + length
    if not count:
        raise KRCError("No timed KRC lyric lines were found.")
    return ET.tostring(root, encoding="unicode")
