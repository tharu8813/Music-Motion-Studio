# Music Motion Studio: release stabilization audit (2026-11-12)

This audit is a **release candidate gate**, not a claim that a public
installer has already been smoke-tested or published.

## Scope and current product policy

- Keep the two supported renderer selections: FFmpeg and automatic Rust GPU.
  Do not add or expose another Rust GPU optimization toggle.
- The previous performance exploration is concluded. No further experimental
  pipe compression, changed-row protocol, extra packet workers or text cache
  behavior is being merged in this stabilization pass.
- Preserve existing \`.pvsproj\` format, installer AppId, user FFmpeg,
  translation models, presets, backup/recovery, project files and app settings.
- Preserve the stable version defined in \`app/__init__.py\` and \`setup.iss\`.
  A new GitHub Release is **not** cut by this PR.

## Release bundle audit

\`playlist_canvas.spec\` intentionally bundles the Windows x64 D3D11/NVENC
worker as \`_internal/native/d3d11_qt_canvas_stream_export.exe\`, and emits
\`integrity.json\` alongside the packaged launcher. The application uses the
native worker in frozen mode, and Maintenance -> Integrity Check relies on
the manifest.

Before this change, \`scripts/verify_distribution.py\` checked the Rust
\`music_motion_native*.pyd\` extension but not the GPU executable or integrity
manifest. An otherwise valid-looking installer could therefore ship without
a working Rust GPU engine, or without a usable built-in integrity check.

The release validator now requires the native worker, requires a parsed
\`integrity.json.files\` map with valid SHA-256 entries for **all expected
static payloads, localized help pictures and discovered native extensions**,
and independently hashes the launcher, bundled Python DLL, and GPU worker
against the manifest. It rejects missing/corrupted artifacts before the
installer can be considered valid. The existing PyInstaller/CI packaging
workflow calls this validator after COLLECT.

The check is not an external signature or a substitute for authenticating
GitHub Release downloads; it is an internal completeness and corruption gate.

## TTML / karaoke Canvas compatibility

The track-details TTML preview already handles both PySide6 bindings of
\`QTextLine.cursorToX(int)\`: one returns a float, the other
\`(x, adjusted_cursor)\`. \`app/canvas/renderers/lyrics_renderer.py\`
previously indexed \`[0]\` unconditionally in \`paint_word_reveal\`, which
could crash when rendering word timing in a binding that returns only a
number. The Canvas renderer now uses one consistent coordinate normalization
for both prefix and overlapping-word highlight paths.

Tests confirm identical pixels between both return formats on mixed
Hangul, non-BMP emoji and Latin text, and exercise real Qt path drawing.
No font shaping, positions or default drawing path are otherwise changed.

## Automated acceptance criteria

Before merging:
1. Actual GUI/lyrics/track-details/renderer and feature regression tests pass
   in the Windows isolated-module CI.
2. Real audio + Qt Canvas packet CI passes.
3. Windows PyInstaller COLLECT finishes, distribution validator passes,
   and the frozen \`Music Motion Studio.exe --smoke-test\` exits successfully.
4. No unexpected changes to native encoder, audio, source media, project
   serialization, runtime dependencies, installer AppId or update behavior.
5. Latest PR head has no blocking reviews or merge conflicts.

## Installer acceptance still requiring a real Windows user session

These checks cannot be certified just by headless CI. Before publishing a
public production release, test on a fresh Windows 11 user account or VM:

- Run Inno Setup as a clean install and as an in-place upgrade from the
  currently published version, retaining the user's project/preferences.
- Launch the packaged application after installation without Rust or Python
  development tools; open an existing \`.pvsproj\` via file association.
- Exercise TTML overlapping words, Korean/emoji lines, quick tab switches,
  maximizing/restoring preview and a paused/closing video preview.
- Export one static+TTML project with FFmpeg and another with Rust GPU
  on the supported NVIDIA host; inspect resulting video and audio.
- Cancel both render engines during setup and midway through mux; confirm
  no stale worker process or partial file gets left behind.
- Test first-run managed FFmpeg installation, local translation dependencies,
  config persistence, auto-updater installer handoff, and repair/integrity
  UI against the actual installed runtime.
- Uninstall/reinstall while verifying that personal files and settings are
  never deleted by the installer.

Do not label the release as "fully ready" until both CI and the external
installer/session checks above have passed. Keep unverified feature gaps
visible instead of masking a failure with an FFmpeg/Rust fallback.
