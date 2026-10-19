@echo off
REM Wrapper the scheduler runs — activates nothing, just calls python.
cd /d "%~dp0"
python digest_agent.py >> digest.log 2>&1
