@echo off
REM Launch Chrome with PrivAgent's ISOLATED agent profile only.
REM Never run agent work in your everyday Chrome profile: the agent profile
REM is a separate --user-data-dir with its own cookies/logins, and its state
REM is wiped on start by the extension (when the isolated-profile flag is set).
setlocal
set "REPO_ROOT=%~dp0.."
if "%PRIVAGENT_AGENT_PROFILE_DIR%"=="" set "PROFILE_DIR=%REPO_ROOT%\backend\data\agent-profile"
if not "%PRIVAGENT_AGENT_PROFILE_DIR%"=="" set "PROFILE_DIR=%PRIVAGENT_AGENT_PROFILE_DIR%"
if not exist "%PROFILE_DIR%" mkdir "%PROFILE_DIR%"
set "CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=chrome.exe"
REM Task 10: tracker / fingerprint hygiene flags. Every flag here is a
REM real, long-established Chrome command-line switch (see
REM docs/ISOLATION.md); no flag is invented. There is NO Chrome flag for
REM fingerprint randomisation — that would need extension-level work, and
REM is documented as a remaining gap rather than faked with a flag.
start "" "%CHROME%" --user-data-dir="%PROFILE_DIR%" --no-first-run --no-default-browser-check --block-third-party-cookies --disable-background-networking --disable-component-update --disable-sync --disable-domain-reliability --no-pings %*
