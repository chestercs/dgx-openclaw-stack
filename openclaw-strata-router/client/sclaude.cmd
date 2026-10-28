@echo off
rem Claude Code against the Strata DeepSeek on a GB10, through strata-router (openclaw-strata-router/README.md).
rem Install: copy to a folder on PATH (e.g. %USERPROFILE%\.local\bin), put the server's STRATA_API_KEY in
rem %USERPROFILE%\.strata\key as plain ASCII (README: Set-Content -NoNewline -Encoding ascii), and set
rem STRATA_ROUTER_URL (e.g. setx STRATA_ROUTER_URL http://192.168.1.50:8096).
rem SCLAUDE_LANGUAGE (optional, e.g. Hungarian): the model follows a language line here better than the one deep
rem inside Claude Code's system prompt. Every model slot is the one local model; the auto-mode classifier gets the
rem alias the router recognises.
setlocal
if not defined STRATA_KEY_FILE set STRATA_KEY_FILE=%USERPROFILE%\.strata\key
if not exist "%STRATA_KEY_FILE%" (echo sclaude: put the Strata API key in %STRATA_KEY_FILE% 1>&2 & exit /b 1)
if not defined STRATA_ROUTER_URL set STRATA_ROUTER_URL=http://127.0.0.1:8096
if not defined STRATA_MODEL_ID set STRATA_MODEL_ID=deepseek-v4.1-flash
if not defined STRATA_CLASSIFIER_ALIAS set STRATA_CLASSIFIER_ALIAS=%STRATA_MODEL_ID%-classifier
if not defined STRATA_MAX_CONTEXT set STRATA_MAX_CONTEXT=131072
set ANTHROPIC_AUTH_TOKEN=
set /p ANTHROPIC_AUTH_TOKEN=<"%STRATA_KEY_FILE%"
if not defined ANTHROPIC_AUTH_TOKEN (echo sclaude: %STRATA_KEY_FILE% is empty 1>&2 & exit /b 1)
set ANTHROPIC_API_KEY=
set ANTHROPIC_BASE_URL=%STRATA_ROUTER_URL%
set ANTHROPIC_MODEL=%STRATA_MODEL_ID%
set ANTHROPIC_DEFAULT_HAIKU_MODEL=%STRATA_MODEL_ID%
set ANTHROPIC_DEFAULT_SONNET_MODEL=%STRATA_MODEL_ID%
set ANTHROPIC_DEFAULT_OPUS_MODEL=%STRATA_MODEL_ID%
set CLAUDE_CODE_SUBAGENT_MODEL=%STRATA_MODEL_ID%
set CLAUDE_CODE_AUTO_MODE_MODEL=%STRATA_CLASSIFIER_ALIAS%
rem a per-request attribution header would change the prompt prefix and defeat Strata's conversation cache
set CLAUDE_CODE_ATTRIBUTION_HEADER=0
set CLAUDE_CODE_TOTAL_TOKENS_REMINDER=off
set CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
set CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1
set CLAUDE_CODE_MAX_CONTEXT_TOKENS=%STRATA_MAX_CONTEXT%
set CLAUDE_CODE_AUTO_COMPACT_WINDOW=%STRATA_MAX_CONTEXT%
set CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=90
rem a long think plus a long answer at ~15-30 tok/s takes minutes
set API_TIMEOUT_MS=3600000
rem Effort: Claude Code sends none for a model it does not know unless told to; the router turns it into a
rem thinking budget (low 2K, medium 8K, high 16K, xhigh/max 24K). Change it in a session with /effort.
set CLAUDE_CODE_ALWAYS_ENABLE_EFFORT=1
if not defined SCLAUDE_EFFORT set SCLAUDE_EFFORT=medium
rem goto, not an if (...) block: an unquoted ")" in the arguments would end the block
if not defined SCLAUDE_LANGUAGE goto plain
claude --effort %SCLAUDE_EFFORT% --append-system-prompt "Always answer the user in %SCLAUDE_LANGUAGE%, whatever language the code, files or tool output are in." %*
exit /b %errorlevel%
:plain
claude --effort %SCLAUDE_EFFORT% %*
