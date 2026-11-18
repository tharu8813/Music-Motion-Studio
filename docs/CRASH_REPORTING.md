# Crash reports and safe shutdown

Music Motion Studio writes application logs under %LOCALAPPDATA%/PlaylistCanvas/logs on Windows.
The newest 50 files under logs/crash-reports are retained.

## Report contents

Each standalone UTF-8 report includes a unique ID, stable signature, UTC time,
app version, OS/build, architecture, CPU/RAM, Python/Qt/PySide6, executable,
process uptime, active project and unsaved status, screens and DPI, Windows
display adapter and driver (when available), Python traceback, error context,
and the last 35 lines of the application log. No command or network call is
made to collect system information.

Privacy: local paths and log messages may contain sensitive information.
Review reports before sharing. Environment variables are not exported.

## Exit options after repeated errors

Two or more errors in 90 seconds enable an Exit options menu:

1. Save project and exit: use the existing blocking save path and close only
   if the save succeeded and no dirty changes remain.
2. Save as and exit: choose a different name and wait for successful save.
3. Exit without saving: request confirmation and skip only the duplicate
   unsaved-project prompt, not worker safety guards.
4. Force quit: require a second confirmation and exit immediately, without
   saving or worker cleanup. Use only as a last resort.

Simply closing the crash dialog does not exit the editor. A cancelled or
failed save leaves the editor open. Project-load failures defer any selected
exit until the load guard is released.

## Restart after a forced or abnormal shutdown

An interrupted previous session is **not treated as a new exception**.
A leftover `running.marker` no longer opens the detailed crash-report dialog,
increments the repeated-crash counter, or generates another crash-report file.
Instead, before the editor and GPU preview widgets are created, startup asks:

- **안전 모드로 실행 / Safe Mode**: use the CPU preview backend for this session,
  skip automatic update checks and background AutoMix analysis, and postpone
  cache/temporary-directory cleanup tasks. Projects and saved user preferences
  remain unchanged. Previously saved autosave/recovery snapshots still use the
  usual project recovery workflow.
- **일반 모드로 실행 / Normal Mode**: run with normal settings and startup tasks.

Closing the choice defaults to Safe Mode. Safe Mode is **session-only**: a clean
restart uses normal settings again. For support and diagnosis, `--safe-mode`
starts directly in Safe Mode without the prompt. `--smoke-test` never opens
a blocking startup dialog.

A running marker still records the session for recovery detection and is
removed by a normal Qt application exit. Actual unhandled exceptions, Qt
critical messages and project-loading errors still receive detailed reports
and keep the repeated-error Exit options menu.

## Native limitations and QA

OS-level kills and native access violations cannot reliably show Qt UI.
Existing native-crash.log and abnormal-shutdown markers remain the fallback.
Reports are saved locally for **actual errors**; they are never automatically uploaded.

Run: python -m unittest -v tests.test_crash_diagnostics
Windows deployment CI also runs Python compileall and all test modules.
Before releasing, manually validate each exit action on a real .pvsproj
and while an export or save worker is active.

## Manual crash injection from Settings

Tools > Settings > Maintenance > Program maintenance > Crash diagnostics test.

The button opens a six-item dropdown: main-thread Python exception-hook
simulation, real background-thread uncaught exception, caught-operation
exception, Qt critical log message, two successive crash reports (exit-menu
testing), and actual abnormal process termination using os.abort().

Each selection needs confirmation. The Settings dialog closes before the
test is dispatched, leaving the editor free to display its crash UI.

The first five choices exercise the application's installed exception/report
pipeline and do not intentionally exit the process. The last choice is
irreversible: it skips saves and worker cleanup and should only be run with a
throwaway project or after saving every edit. The next-start **safe-start choice** and native-crash.log are its diagnostics.

Test each nonfatal mode, then validate the repeated-report Exit options
menu. Only perform the native abort test manually in a disposable Windows
session. The normal CI tests mock dangerous termination.
