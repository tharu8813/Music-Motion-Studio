# P2: Bundled AutoMix model and localized resource release integrity (2026-11-15)

Tracked by [issue #136](https://github.com/tharu8813/Music-Motion-Studio/issues/136).

## Inventory and rationale

The three tracked, bundled ONNX files are used by the AutoMix feature. Removing any of them would silently reduce capability and is **not** a packaging-size optimization:

| Asset | Git tracked size | Purpose | Bundled notice/license |
| --- | ---: | --- | --- |
| `beat_this_final0_int8.onnx` | 22,975,889 bytes | Beat and downbeat analysis | `BEAT_THIS_LICENSE.txt` (MIT) |
| `umxhq_vocals_int8.onnx` | 9,040,017 bytes | Vocal-section detection | `OPEN_UNMIX_LICENSE.txt` (MIT) |
| `yamnet.onnx` | 16,093,355 bytes | Music/audio classification | `YAMNET_LICENSE.txt` (Apache-2.0) and `YAMNET_NOTICE.md` |

All three together: **48,109,261 bytes** (about 45.88 MiB). YAMNet also depends on the 13,574-byte `yamnet_class_map.csv` (AudioSet CC BY 4.0 attribution in the notice). These licenses, notices and class map are already named in `AUTOMIX_MODEL_FILES` in `playlist_canvas.spec` and in `REQUIRED_FILES` in `scripts/verify_distribution.py`. `PACKAGING.md` already records the purpose of each model. No tracked weights, notices, licenses or resources were changed in this P2 pass.

## The actual release-gate gap

Previously, distribution verification required models, licenses and language/help resources to **exist** and required `integrity.json` to hold a valid-looking SHA-256 entry for them, but independently checked the stored bytes only for the EXE, Python DLL and GPU worker. A damaged ONNX graph, mismatched CSV, license, `ko.json`/`en.json` or help image could therefore pass the release distribution check after packaging.

Now `_verify_integrity_manifest` hashes the bytes of **all required** tracked distribution payloads, localized help images and discovered native extensions against their manifest entries. A mismatch fails the release acceptance gate. Tests simulate post-manifest corruption in all three ONNX files, model notices/licenses, YAMNet labels, translations and a help picture. The existing Windows install acceptance workflow already runs `scripts/verify_distribution.py` on the built and installed distribution.

This increases only the release verification's read/hash work; it does not change product startup, model inference, installed files, FFmpeg/Rust export, or the user-facing performance profile.

## Security and release limits

The release manifest is generated from the built distribution. Comparing a file to that manifest proves **internal package consistency at validation time**, not authenticity of upstream downloaded model weights, not legal clearance for newly added model files, and not independent supply-chain verification. The pinned upstream YAMNet origin/revision and source checksums remain documented separately in `YAMNET_NOTICE.md`. For a model replacement, re-check upstream provenance, license and independent checksum before updating the tracked binary; do not regenerate checksums to conceal a provenance mismatch.

The actual consumer Windows 11 install/upgrade and GPU 1080p60/full-cancel release acceptance items in #136 remain open. This P2 change is not a public-release approval.
