@echo off
REM Registers a daily 8:00 AM task in Windows Task Scheduler.
REM Run this once from the ai-digest-agent folder (as your normal user).

schtasks /Create ^
  /TN "AI News Digest" ^
  /TR "\"%~dp0run_digest.bat\"" ^
  /SC DAILY ^
  /ST 08:00 ^
  /F

echo Task "AI News Digest" registered for 8:00 AM daily.
