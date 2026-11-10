"""Fail-closed video contract until a production frame decoder is connected.

Qt preview's asynchronous mailbox cannot guarantee an exact CFR seek. Export
must supply decoded media frames, masks and color filters before enabling VIDEO.
The existing engine uses build_video_occurrences and FFmpeg's video filters.
"""


def video_preflight_reason(source):
    return (
        f"{source.name or source.id} [video; {source.id}]: "
        "native_video_frame_provider_unavailable (exact CFR decode/seek, "
        "repeat/speed, masks and color filters are not connected). "
        "Use the existing FFmpeg export engine for the entire project."
    )
