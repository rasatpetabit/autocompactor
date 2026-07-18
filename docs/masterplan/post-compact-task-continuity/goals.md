topic: Strengthen post-autocompactor multi-step task continuity via a mechanical progress ledger and gated hard resume.

## G1: Correct mid-wave hard resume
After compact mid-masterplan wave with session affinity, autonomous next-step brief names the correct task id/wave/files and instructs resume-not-restart.
signal: test
evidence: progress_lib + bridge + shim tests with masterplan fixture and affinity-true session JSONL asserting nextStepSource=progress:masterplan and brief contains task id + RESUME header

## G2: No stale-plan hijack
An in-progress plan present in the repo that the session is not executing must not produce autonomous hard resume.
signal: test
evidence: affinity-false fixture: progress may appear in digest but progressResume is advisory/off and no autonomous coding triggerTurn

## G3: Wait-path regression free
Waiting-state resume remains wait-shaped — no coding triggerTurn when mode=wait or open_work waiting is the winner.
signal: test
evidence: co-presence wait+masterplan fixture + existing test_open_work / shim_wait_resume still green; nextStepWait true

## G4: Content-free telemetry
Progress telemetry never stores brief or summary text — only ids, ranks, modes, lengths, affinity/confidence flags.
signal: test
evidence: prepare/reinject log_event assertions that forbidden keys/text are absent from event payloads

## G5: Verification green
Full pytest suite, open_work/wait regressions, and Pi smoke pass after the change lands.
signal: command
evidence: python3 -m pytest tests/ -q and PI_SMOKE=1 bash tests/smoke_test_pi.sh cited at finish
