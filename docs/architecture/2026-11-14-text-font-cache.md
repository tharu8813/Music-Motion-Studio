# P1: Qt typography template reuse (2026-11-14)

The [Qt text-stage profile](2026-11-12-native-qt-text-stage-profile.md)
observed significant text/lyrics paint costs but did not identify the
font-construction fraction. This PR is a bounded, pixel-preserving candidate,
**not an established end-to-end speed improvement**.

`SourceItem.text_font` now caches at most 64 font templates per item,
keyed by family, size, weight, italic, letter spacing and capitalization.
It returns `QFont(template)`, a detached-on-write Qt implicit-shared copy,
so callers can safely customize row fonts without contaminating other rows.
Applying source settings clears cached templates after optional custom font
loading, and direct dynamic property changes are reflected in the full key.

CI uses Windows Qt tests to compare `QFont.key()`, text advance metrics and
actual raster BGRA pixels against the previous uncached implementation,
including Hangul/Latin text, changed typography, caller mutations, and
bounded size. Existing Canvas, lyric, animation, export and video regression
tests remain the approval gate.

For performance validation, run the same hardware project A-B-B-A at
1080p60/30s comparing a build without this change to the new build.
Document timing plus complete decoded-video/audio SHA parity. Do not claim
real speedup without that evidence. No GPU optimization UI switches added.
