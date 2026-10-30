@echo off
rem Starts Jekyll from the real site root - walks UP from wherever this
rem .bat lives until it finds _config.yml, however many levels that takes,
rem instead of only checking one level up. That one-level check is how
rem this got run from inside ref/ before (with no _config.yml there),
rem which is exactly how ref/_site ended up full of copied screenshot
rem folders and stray .bat files - Jekyll happily builds from cwd with
rem default settings when it can't find a config, it just silently builds
rem the wrong folder instead of erroring.

cd /d "%~dp0"

rem The port this dev server binds to - kept in one place so the kill-check
rem below and the actual `jekyll serve` invocation at the bottom can never
rem drift out of sync with each other.
set JEKYLL_PORT=4000

set DEPTH=0
:findroot
if exist "_config.yml" goto foundroot
set /a DEPTH+=1
if %DEPTH% GEQ 10 goto notfound
cd ..
goto findroot

:notfound
echo Could not find _config.yml above "%~dp0" - put this .bat in or below the site folder.
pause
exit /b 1

:foundroot
echo Site root: %cd%
echo.
rem A stale PATH in whatever launched this window is the #1 reason
rem "Last updated" silently falls back to build time instead of the real
rem git commit time (see _plugins/last_commit.rb) - print what THIS
rem window actually resolves, so that's visible instead of silent:
echo Checking git/ruby/bundle on PATH for this window:
where git
where ruby
where bundle
echo.

rem Write the real last-git-commit info (time, short hash, branch) to
rem _data\last_commit.yml before every local serve. _plugins\last_commit.rb
rem does the equivalent git calls from inside Jekyll itself (meant to work
rem the same way locally and in CI/production) but on this machine
rem specifically, Jekyll's plugin loader never actually runs anything in
rem _plugins\ under the real `jekyll` CLI (confirmed directly - a plain
rem top-level print statement in that file never fires under `jekyll
rem build`/`serve` here, even though the exact same file loads fine when
rem Jekyll is driven from a plain Ruby script instead of the CLI - root
rem cause not fully pinned down, `where` output above is one more data
rem point toward it). This batch-level fallback sidesteps the whole
rem mystery, so it's what actually drives the header banner locally.
rem _data\last_commit.yml is gitignored - never committed - so production/
rem CI (no start-jekyll.bat step there) just falls back to the plugin's own
rem values instead, where they aren't known to be broken. See
rem _includes/header.html for the fallback chain (this data file -> the
rem plugin's values -> build time, in that order).
if not exist "_data" mkdir "_data"
set COMMIT_TIME=
set COMMIT_HASH=
set COMMIT_BRANCH=
set SERVER_START=
for /f "delims=" %%T in ('git log -1 --format^=%%cI 2^>nul') do set COMMIT_TIME=%%T
for /f "delims=" %%T in ('git log -1 --format^=%%h 2^>nul') do set COMMIT_HASH=%%T
for /f "delims=" %%T in ('git rev-parse --abbrev-ref HEAD 2^>nul') do set COMMIT_BRANCH=%%T

rem Host OS tag (see _plugins/last_commit.rb's host_os_tag for the full
rem explanation - inspired by AzerothCore's own revision-line platform tag,
rem but named "Windows"/"Mac"/"Unix" rather than AC's literal Win64/Win32/
rem Unix, since the 32-vs-64-bit distinction isn't a meaningful detail for
rem a static site). This .bat only ever runs on Windows, so it's just the
rem literal string - set here (not hardcoded below) purely so it sits right
rem next to the git calls above as the obvious Windows-side counterpart to
rem the Ruby plugin's own version. Computed unconditionally, same reasoning
rem as the plugin: AzerothCore's platform tag isn't gated on git
rem succeeding, so this shouldn't be either.
set HOST_OS=Windows
rem "Uptime" in the banner counts from THIS moment - when this server
rem actually started - not from the commit time above (those are two
rem different things: how fresh the displayed revision is vs. how long
rem this dev server has been running). Get it via PowerShell instead of
rem %date%/%time% because those are locale-formatted and unreliable to
rem parse; ToString with an explicit format is locale-independent and
rem already ISO 8601 with a numeric UTC offset, same as git's %%cI above.
rem Run via `start /min /wait` into its OWN separate console, and have
rem PowerShell itself write the result straight to a temp file (Out-File),
rem rather than piping it inline through `for /f` like the git calls
rem above - piping keeps everything in THIS window's shared console, and
rem powershell.exe applies its own registered font/size to whatever
rem console it attaches to, which was visibly resizing/refonting this
rem window mid-run. A separate console confines that to itself instead.
set "GEARS5_TMP=%TEMP%\gears5-server-start.txt"
if exist "%GEARS5_TMP%" del "%GEARS5_TMP%" >nul 2>nul
start "" /min /wait powershell -NoProfile -Command "(Get-Date).ToString(\"yyyy-MM-ddTHH:mm:sszzz\") | Out-File -Encoding ascii -NoNewline '%GEARS5_TMP%'" 2>nul
if exist "%GEARS5_TMP%" (
    set /p SERVER_START=<"%GEARS5_TMP%"
    del "%GEARS5_TMP%" >nul 2>nul
)
if defined COMMIT_TIME (
    > "_data\last_commit.yml" echo time: "%COMMIT_TIME%"
    if defined COMMIT_HASH >> "_data\last_commit.yml" echo hash: "%COMMIT_HASH%"
    if defined COMMIT_BRANCH >> "_data\last_commit.yml" echo branch: "%COMMIT_BRANCH%"
    if defined SERVER_START >> "_data\last_commit.yml" echo server_start: "%SERVER_START%"
    >> "_data\last_commit.yml" echo os: "%HOST_OS%"
    echo Last commit for header: %COMMIT_HASH% %COMMIT_TIME% (%COMMIT_BRANCH% branch^) (%HOST_OS%^)
) else (
    echo Warning: could not read the last git commit via git - the header banner will fall back to build time.
    if exist "_data\last_commit.yml" del "_data\last_commit.yml"
    if defined SERVER_START (
        > "_data\last_commit.yml" echo server_start: "%SERVER_START%"
    )
    rem OS still gets written even with no git info at all (source
    rem downloaded/extracted with no .git folder) - same as AzerothCore's
    rem own platform tag, which isn't gated on git succeeding either.
    >> "_data\last_commit.yml" echo os: "%HOST_OS%"
)
if defined SERVER_START echo Server started: %SERVER_START%
echo.

rem Kill anything already listening on JEKYLL_PORT before starting a new
rem server - the #1 reason edits stop showing up in the browser is a
rem previous `jekyll serve` window getting closed (or this .bat getting
rem re-run) without the old ruby.exe actually dying, so it just keeps
rem answering requests with whatever it had built before, forever, while
rem a second server starts up (or fails to bind) alongside it completely
rem unnoticed. Match on ":<port> " (trailing space) rather than "<port>"
rem alone so this can't misfire on a port that merely CONTAINS these
rem digits (e.g. 14000 or 40001) - netstat's own column spacing guarantees
rem the real port is always followed by whitespace before the next column.
echo Checking for anything already listening on port %JEKYLL_PORT%...
set FOUND_OLD=0
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /c:":%JEKYLL_PORT% " ^| findstr /c:"LISTENING"') do (
    echo   Stopping old server (PID %%P^)...
    taskkill /F /PID %%P >nul 2>nul
    set FOUND_OLD=1
)
if "%FOUND_OLD%"=="1" (
    rem Give Windows a moment to actually release the socket before the
    rem new `jekyll serve` below tries to bind the same port - killing the
    rem process is not always instant from the OS's point of view.
    timeout /t 1 /nobreak >nul
) else (
    echo   Nothing was listening on port %JEKYLL_PORT%.
)
echo.

bundle exec jekyll serve --host 0.0.0.0 --port %JEKYLL_PORT%
pause
