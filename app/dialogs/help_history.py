"""Revision history for the bilingual in-app help.

When updating an article, move its ID to a row for the new app version and
modification date. Keep its added date, and update its written date when
the text changes. Screenshots and translations are reviewed together.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class HelpRevision:
    added_on: str
    written_on: str
    modified_on: str
    version: str


# Added, last text edit, last update, app version, topic IDs.
_HISTORY = (
    ("2026-09-29", "2026-09-29", "2026-11-16", "1.4.1.0",
     """sources layers audio_visuals canvas_shortcuts preview_mix
      transition_details lrc_audio lrc_input lrc_timing lrc_review lrc_recovery automix_editor
      automix_styles automix_timeline automix_properties automix_listen automix_reuse
      automix_shortcuts track_analysis maintenance language_packs"""),
    ("2026-09-29", "2026-10-25", "2026-11-16", "1.4.1.0",
     """canvas_editing"""),
    ("2026-09-29", "2026-10-29", "2026-11-16", "1.4.1.0",
     """lrc_generator lrc_shortcuts"""),
    ("2026-09-29", "2026-10-30", "2026-11-16", "1.4.1.0",
     """workspace troubleshooting"""),
    ("2026-09-29", "2026-11-04", "2026-11-16", "1.4.1.0",
     """timeline"""),
    ("2026-09-29", "2026-11-14", "2026-11-16", "1.4.1.0",
     """automix"""),
    ("2026-09-29", "2026-11-15", "2026-11-16", "1.4.1.0",
     """properties lyrics track_audio ffmpeg"""),
    ("2026-09-29", "2026-11-16", "2026-11-16", "1.4.1.0",
     """start text_tokens video_source animation playlist project_content full_preview preview_controls
      preview_quality lyrics_files track_details track_lyrics projects project_settings export export_process
      performance settings updates shortcuts"""),
    ("2026-10-06", "2026-10-06", "2026-11-16", "1.4.1.0",
     """music_reaction"""),
    ("2026-10-06", "2026-11-15", "2026-11-16", "1.4.1.0",
     """lyrics_transition lyrics_intro lyrics_line_styles"""),
    ("2026-10-06", "2026-11-16", "2026-11-16", "1.4.1.0",
     """mp3_metadata"""),
    ("2026-10-29", "2026-10-29", "2026-11-16", "1.4.1.0",
     """lyrics_online_search source_decoration status_warnings"""),
    ("2026-10-29", "2026-11-03", "2026-11-16", "1.4.1.0",
     """lyrics_wrapping"""),
    ("2026-11-02", "2026-11-16", "2026-11-16", "1.4.1.0",
     """lyrics_translation"""),
    ("2026-11-03", "2026-11-03", "2026-11-16", "1.4.1.0",
     """legacy_keyframes background_effects"""),
    ("2026-11-03", "2026-11-04", "2026-11-16", "1.4.1.0",
     """custom_motion motion_shortcuts"""),
    ("2026-11-14", "2026-11-14", "2026-11-16", "1.4.1.0",
     """export_engines"""),
)

HELP_HISTORY = {
    identifier: HelpRevision(added_on, written_on, modified_on, version)
    for added_on, written_on, modified_on, version, identifiers in _HISTORY
    for identifier in identifiers.split()
}
