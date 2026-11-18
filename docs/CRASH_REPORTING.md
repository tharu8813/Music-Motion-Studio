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

## Native limitations and QA

OS-level kills and native access violations cannot reliably show Qt UI.
Existing native-crash.log and abnormal-shutdown markers remain the fallback.
Reports are saved locally; they are never automatically uploaded.

Run: python -m unittest -v tests.test_crash_diagnostics
Windows deployment CI also runs Python compileall and all test modules.
Before releasing, manually validate each exit action on a real .pvsproj
and while an export or save worker is active.
