# M Focus for Windows

Same app, same look (dark theme, ring timer, Inter font bundled), now for Windows.
* **MFocus-Win10-11.exe** - Windows 10 / 11 (identical to the Linux look)
* **MFocus-Win8.1.exe**   - Windows 8.1 and newer (also fine on 10 / 11)
64-bit Windows only.

## Get the .exe from GitHub
1. Upload everything in this folder to a GitHub repository (keep the hidden `.github` folder!).
2. **Actions** tab -> **Build Windows exe** -> **Run workflow**. Wait ~5 minutes for the green ticks.
3. Open the finished run -> **Artifacts** -> download `MFocus-Win10-11` (and/or `MFocus-Win8.1`) -> unzip.
   (Push a tag like `v1.0` and the exes are also attached under **Releases**.)

## Using it
Double-click the exe. Starting a session (and ending one early) shows the Windows permission
prompt (UAC) - click **Yes**. While a focus phase runs, the blocked sites are blocked and the
listed apps are closed; break phases open everything again. It keeps working if you close the
window or restart the PC.

Apps are matched by program name: `discord` closes `Discord.exe`, `steam` closes `steam.exe`.
Apps are closed forcefully (like "End task"), so save your work in them first.

## How it works
* The block is written to `C:\Windows\System32\drivers\etc\hosts` and re-checked every 2 seconds
  by a background task (Task Scheduler, runs as SYSTEM). History/state: `C:\ProgramData\MFocus`.
  Your settings: `%APPDATA%\MFocus\config.json`.
* The background process refuses "End task" in Task Manager, and Windows restarts it within a
  minute if it is stopped anyway. (A determined admin with debugging tools can still stop it.)

## Notes
* Defender / SmartScreen may warn about an unsigned exe or hosts-file changes:
  "More info" -> "Run anyway".
* Windows 8.1 needs the normal Windows Updates (Universal C Runtime).
* Firefox with "DNS over HTTPS" turned on can ignore the hosts file; turn it off.
* Remove everything: delete `C:\Program Files\MFocus` and `C:\ProgramData\MFocus`, then in an
  admin Command Prompt run `schtasks /Delete /TN MFocusEnforcer /F`.
