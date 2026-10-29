# Music Motion Studio external language packs

Music Motion Studio can load data-only UTF-8 JSON language packs, a feature
introduced in Playlist Canvas 1.0.1.
Use **Settings → General → External language packs → Import** or place a file
in the folder opened by **Open folder**, then choose **Reload**.

On Windows, Music Motion Studio automatically creates the following folder and
three language files on first launch:

```text
%LOCALAPPDATA%\PlaylistCanvas\languages\
```

- `ko.json`: built-in Korean reference
- `en.json`: built-in English reference
- `language-pack-template.json`: a new-language template whose translation
  values are blank

The blank template contains every stable translation key and every English
literal found in the application source, including dynamic message templates.
Copy it before editing, rename the copy to its locale (for example
`fr-FR.json`), and update its metadata. Empty values safely fall back to English,
so a language pack can be translated gradually. The three default files are
ignored by the external-language scanner and never appear as duplicate entries.

## Format

- `schema_version` must currently be `1`.
- `metadata.locale` uses a language tag such as `ja-JP`, `zh-CN`, or `es-ES`.
- `strings` translates stable application keys. Missing keys fall back to English.
- Empty translation values also fall back to English.
- `overrides` maps an existing English UI string to its translation. This keeps
  older screens translatable while they are migrated to stable keys.
- Every `{placeholder}` in a literal source must appear unchanged in its
  translation.
- Language packs are JSON data only. Python, JavaScript, commands, and plugins
  are not loaded or executed.

Built-in locale codes `ko` and `en` are reserved. Packs larger than 4 MB,
incompatible schemas, invalid JSON, duplicate locales, unsafe metadata, or a
`minimum_app_version` newer than the running application are ignored.

The included [`ja-JP.example.json`](ja-JP.example.json) is a small working
example rather than a complete Japanese translation. Copy it, update the
metadata and translations, then rename it to `<locale>.json`.

## Compatibility

New application versions may add keys. Existing packs remain usable because
missing entries use the built-in English fallback. Pack authors should update
`version` when publishing a revision and set `minimum_app_version` only when
the translation depends on strings introduced by a newer application.
