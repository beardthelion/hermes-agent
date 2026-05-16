# CLAUDE.md — Hermes Agent + Sendblue Gateway Adapter

This file gives any Claude instance the context needed to operate on this project safely. Read it at the start of every session.

## Operator

Beardy / Beardthelion. Tuscaloosa, AL. AV specialist by day, Bitcoin day trader, building self-hosted AI infrastructure.

Workflow is a relay: Claude proposes, user reviews, Beardy (the Hermes agent on Telegram) applies after greenlight on the VPS, Beardy pastes raw output, user relays to Claude. Path 2 (user direct SSH) is used when verification involves raw output Beardy is likely to summarize.

## Infrastructure

VPS: 129.213.39.57, SSH as ubuntu. Subdomain beard-hermes.duckdns.org.

Hermes: Fork of NousResearch/hermes-agent at github.com/beardthelion/hermes-agent. Deployed at ~/.hermes/hermes-agent. User-systemd-managed via hermes-gateway.service. Deployment branch is production with an ExecStartPre branch gate that refuses to start if the tree is not on production.

Bridge (retired 2026-05-14): /opt/sendblue-bridge/bridge.py, ~1500+ LOC. Stopped + disabled via systemctl; webhook unregistered from Sendblue. Files retained on disk for historical reference only. iMessage delivery is now sole-routed through the gateway-native Sendblue adapter. Allowed number: +17706768883. Sendblue from: +16232843671.

## Architecture Overview

### Long-term goal

gateway/platforms/sendblue.py upstream PR to NousResearch/hermes-agent so non-Mac users get iMessage access via Hermes natively. The bridge is transitional. Modeled after the gateway/platforms/bluebubbles.py adapter pattern.

### Current strategy: dogfood gateway-native locally first

Cutover complete 2026-05-14. Adapter is sole iMessage receiver; bridge is retired. Pattern B parallel dogfooding ran for sessions 4-5 before cutover. Next step: file upstream PR to NousResearch/hermes-agent after extended sole-receiver validation.

### What transfers from bridge to adapter (Sendblue API specifics)

- Webhook signature verification + inbound message parsing
- Send-message parameter mapping (content, media_url, send_style)
- Sendblue CDN media upload (`/media/objects`)
- Allowed-number validation + rate limit headers

### What gets thrown away (~60% of bridge.py)

Gateway-native plumbing inherits these from base.py:

- Session ID generation, conversation state, SSE streaming, poke dispatch
- SQLite session overrides, day-suffixed session IDs
- Auto-recovery, slash command routing, cron broadcast
- Typing keepalive, progress pokes, silence watchdog
- /memory, /quota bridge-local commands
- Voice transcription as hardcoded Groq Whisper (will register Groq STT in plugin system instead)
- Read receipts / status messages / degraded mode

### Branch model (three-branch pattern)

- main — tracks origin/main, pristine upstream reference. Pulled selectively. Often many commits behind upstream. Never commit directly.
- production — deployed branch. Local-only changes plus selective upstream merges/cherry-picks. Branch gate enforces hermes-gateway only runs from here.
- Upstream PR branches (e.g. fix/foo-bar) — branched off origin/main directly so the diff is clean against upstream.
- Feature branches for local dogfooding (e.g. feat/sendblue-adapter) — branched off production. NOT pushed. Stay local until ready to either merge to production or rebase onto origin/main for upstream PR.

Rule: upstream PR branches awaiting review are NOT merged into production locally. Rebases on maintainer feedback would invalidate any local merge.

Branch-off decision tree:

- Deploying locally → branch off production
- Filing upstream PR → branch off origin/main (use git checkout -b foo origin/main, not local main)

### Authoritative source of truth

- /opt/sendblue-bridge/DEPLOYMENT.md — what's actually deployed on the bridge. Read at start of every debugging session. Update after every deploy.
- ~/.hermes/plans/sendblue-adapter-design.md — five design questions resolved.
- ~/.hermes/plans/sendblue-adapter-architecture.md — eight architectural sections, 515 lines, fully synchronized with code through commit ac5c1d509.

## What's Built So Far

### Retired bridge features (historical reference — bridge offline as of 2026-05-14)

The following ran inside bridge.py until cutover. Some have gateway-native replacements; some are gaps awaiting upstream work. **`/quota` was ported into `gateway/platforms/sendblue.py` as a platform-native slash command 2026-05-14 (commits 4cee5b1ec + dd1f858a7).** `/memory` is NOT currently available — pending native upstream PR.

Phase 2 macro estimation and Phase 3 running-totals were ported to standalone scripts before cutover: see `~/.hermes/scripts/phase2-macro.py`, `phase3-totals.py`, and `fit-log-watcher.py` (the trigger). The fit-log-watcher.service runs these on inotify-style polling of `~/kb/health/fit-log.md`, fully independent of the bridge.

- /memory (reads ~/.hermes/memories/MEMORY.md and USER.md, formatted SMS response with capacity warnings)
- /quota (Sendblue API usage bar)
- Voice transcription (Groq Whisper, audio→text in bridge before Hermes sees it)
- Streaming progress pokes (consumes Hermes SSE, throttled poke phrases for tool events)
- Session overrides table (SQLite, midnight expiry, persists /compress session rotations)
- Day-suffixed session IDs (deterministic per phone+date)
- Typing keepalive (cold-start gap filler, stops when first poke arrives)
- Status messages, read receipts, degraded-mode fallback
- Bridge silence watchdog (Track 1a, deployed 2026-05-04): label-aware templated pokes for 9 tools using existing label field in hermes.tool.progress; SSE keepalive watchdog fires "still …" pokes after 30s of tool execution, 60s minimum gap between same-tool keepalives. PokeThrottle is intentionally bypassed in the keepalive branch (`KEEPALIVE_MIN_INTERVAL_SECONDS` is sole gate). DO NOT add `PokeThrottle` back into the keepalive branch.
- iMessage effects (deployed 2026-05-09): SENDBLUE_DEFAULT_SEND_STYLE env var + send_style param on send_reply() and /admin/broadcast. 13 valid styles validated via frozenset (Sendblue accepts arbitrary strings at API gate — client-side validation is load-bearing). Pokes/system messages stay unstyled by design.

### Upstream features deployed locally

- PR #17178 (slash command base): /help, /new, /reset, /title, /status, /usage, /retry, /undo
- PR #18512 (slash extras): /profile, /branch, /resume
- Teknium PR #12969: multimodal content normalization (image meal logging works without #18597)

### Upstream PRs filed by user (awaiting maintainer review, NOT merged to production locally)

- #17136 — PLATFORM_HINTS for api_server
- #18475 — SessionResetPolicy
- #18597 — decide_image_input_mode (image works locally via #12969 + vision-capable model)
- #18975 — audio routing (voice works locally via bridge Groq Whisper)
- #19041 — /compress (falls through to LLM locally — no api_server handler deployed)
- #20051 — docs(web_tools) summarizer timeout comment
- #22828 — feat(api_server) forward tool result fields in hermes.tool.progress completed events. Extensible whitelist pattern (`_TOOL_RESULT_FIELDS`), image_generate → "image" first entry. Follow-up commit a86fcd147 shipped addressing Code Claude audit (key collision guard, size constraints, 2 new tests). 144 tests passing under scripts/run_tests.sh. Prerequisite for outbound media in Sendblue adapter.
- ~~#27141~~ — feat(gateway) `{args}` substitution in exec-type quick commands. **Closed 2026-05-16 as duplicate of open #9942** (GusBot69, open since 2026-04-15, append-style args). Also duplicates closed #16316, #18195, #20487. Comment on #9942 documents the placeholder-vs-append tradeoff. Production keeps both underlying commits (`01bfc0c78` feature + `a31dd9862` shell-quoting) — locally useful regardless of upstream outcome. Don't refile; if #9942 lands, our production diff against origin/main shrinks by both commits. Local branch `feat/quick-command-args-substitution` (tip `51812249f` with refactor + 12 tests) retained for reference; fork remote branch deleted. Lesson: search open PRs first before filing — three prior closures (#16316/#18195/#20487) plus #9942 were all discoverable with `gh pr list --search "quick command args"`.

### Sendblue gateway adapter — current state

Merged to production: feat/sendblue-adapter merged via commit 56bf43864 (2026-05-13). 28 commits from feature branch. **Live as sole iMessage receiver since 2026-05-14 cutover.** config.yaml `platforms.sendblue.enabled: true`. Webhook URL `https://beard-hermes.duckdns.org/sendblue-gateway/receive` is the only one registered with Sendblue.

Files:

- gateway/platforms/sendblue.py — ~1354 lines
- tests/gateway/test_sendblue.py — ~1278 lines

MVP code (sessions 1-2, feature complete): all four abstract method bodies real:

- get_chat_info() — DM stub
- _handle_webhook() — signature verify, JSON parse, per-item loop
- send() — markdown strip, multi-bubble split, truncation, per-chunk POST, retryable bifurcation
- send_image() + _is_public_image_url() — URL passthrough via media_url field, fallback to base class for non-public URLs

Phase B test backfill (session 3, complete): 27 tests across 6 classes, all green via ./venv/bin/python -m pytest tests/gateway/test_sendblue.py:

- TestSendblueSignatureVerification (4 tests)
- TestSendblueWebhookRouting (3 tests)
- TestSendblueWebhookParsing (5 tests)
- TestSendblueMediaDownload (4 tests)
- TestSendblueOutboundSend (5 tests)
- TestSendblueSendImage (6 tests)

Two latent crashes caught and fixed during Phase B (both would have crashed on first production webhook in Phase A — B-before-A paid for itself):

- self.SIGNATURE_HEADER at line 468 — constant is module-level. Fixed to bare-name reference.
- _handle_webhook used web.json_response / web.Response without importing aiohttp.web. Fixed by adding from aiohttp import web inside _handle_webhook matching BB pattern (BB has it at both BB:163 and BB:769).

Phase C arch doc edits complete: 8 original + 4 followup edits. Arch doc at 515 lines, fully synchronized with code through ac5c1d509. No accumulated drift going into next session.

Phase A operational deployment (session 4, 2026-05-13, complete): live round-trip verified.

- config.yaml: sendblue platform block added with credentials from bridge .env (api_key_id, api_secret, sendblue_number, webhook_public_url, webhook_secret)
- Caddyfile: /sendblue-gateway/* → 127.0.0.1:8665 route added, hot-reloaded
- Webhook auto-registered via _register_webhook() during connect()
- Pattern B parallel test confirmed: both bridge and adapter received webhooks, both returned 200, both generated responses
- Auth fix: SENDBLUE_ALLOWED_USERS=+17706768883 added to ~/.hermes/.env (phone was misrecorded as +17766768883 — 770 not 776)
- Hermes secret redaction (RedactingFormatter) masks phone numbers in logs, making mismatches invisible. Diagnosed by writing raw bytes to /tmp file to bypass log redactor.
- systemd unit env vars get silently reverted by gateway's self-update hook (ExecStart --replace rewrites the unit file). Use ~/.hermes/.env for persistent env vars, not the unit file.
- Adapter disabled after successful test: webhook unregistered from Sendblue, config.yaml enabled: false. Bridge remains primary.

Phase A cutover (session 5, 2026-05-14): adapter re-enabled, bridge webhook unregistered, sendblue-bridge.service stopped + disabled. Three round-trips verified across the cutover boundary: ping/ping2 = 2 SMS each (Pattern B), ping3 = 1 SMS (adapter only). Gateway logs are silent at INFO level by default — adapter return-200 in ~1ms via Caddy access log is the ground-truth signal, not journalctl. Required env-var fix: `_apply_env_overrides` at gateway/config.py:1716-1734 unconditionally overwrites `sendblue_number` and `webhook_public_url` with `os.getenv(..., "")` when API key/secret env vars are present — so `~/.hermes/.env` needs `SENDBLUE_NUMBER=+16232843671` and `SENDBLUE_WEBHOOK_PUBLIC_URL=https://beard-hermes.duckdns.org/sendblue-gateway/receive` even though those values also live in config.yaml `extra:`.

Session 6 audit + feature pass (2026-05-15): adapter audited against bluebubbles.py; six commits on production closing BB-parity gaps and the two audit bugs. 82 tests passing.

- `758c69823` — `send_style` port (13-style frozenset, env var `SENDBLUE_DEFAULT_SEND_STYLE`, per-call override via `metadata["send_style"]`, invalid styles dropped at WARNING). Confetti confirmed live.
- `e52a41728` — outbound media via `/api/upload-file` (multipart upload, returns `media_url`). Adds `send_image_file`, `send_voice`, `send_video`, `send_document`, `send_animation`. `.caf` files render as native iMessage voice memos per Sendblue's extension-based routing. Unblocks `auto_tts` → `play_tts` → `send_voice`.
- `bc3f4af24` — `_verify_signature` now uses `hmac.compare_digest`; `send()` returns `success=False` when `multi_bubble_split` produces zero chunks.
- `d971beff8` — inbound audio/video/document caching via `cache_audio_from_bytes` / `cache_document_from_bytes` (was warns-and-drops). `_ext_to_mime` expanded with audio + video MIME types.
- `076c5d9da` — group chat support. Inbound: non-empty `group_id` flips `chat_type="group"`, chat_id becomes group_id, `group_display_name` populates `chat_name`. Outbound: `_is_group_chat_id` (UUID-ish vs `+E.164`) routes to `/api/send-group-message` with `group_id` field. `mark_read` and `send_typing` skip cleanly for groups (no documented per-group APIs).
- `cd1cfab3d` — `_get_sendblue_day_key` returns UTC `...Z` ISO (was local with offset); fire-and-forget `mark_read` task now registered in `self._background_tasks` with discard callback.

Endpoint cheat sheet (from Sendblue docs, locked in this session):

- `POST /api/upload-file` — multipart, field `file`, returns `{media_url, mediaObjectId}`. 100 MB cap.
- `POST /api/send-group-message` — required: `content`, `from_number`. Optional: `group_id`, `numbers[]`, `media_url`, `seat_id`. Reply to existing group = pass its `group_id`.
- Inbound webhook fields used: `is_outbound`, `sendblue_number`, `from_number`, `content`, `media_url`, `message_handle`, `group_id`, `group_display_name`, `participants`.

### BlueBubbles adapter recon notes (reference for Sendblue work)

gateway/platforms/bluebubbles.py:

- Class: BlueBubblesAdapter(BasePlatformAdapter), platform = Platform.BLUEBUBBLES
- Required: send(). Optional overrides: send_image, send_voice, send_video, send_document, send_typing, stop_typing, mark_read, get_chat_info, format_message, play_tts, connect, disconnect
- Inbound media at line 683-741: _download_attachment() → cache_image_from_bytes / cache_audio_from_bytes / cache_document_from_bytes from base.py
- Cached path reaches agent via MessageEvent(media_urls=...) at line 925 → handle_message()
- Outbound media: send_image / send_voice / send_video / send_document all route through _send_attachment() at line 461 (multipart upload to `/api/v1/message/attachment`)
- play_tts not overridden — inherits base default at base.py:1862-1874 which calls self.send_voice()
- No slash commands implemented — base gateway handles all dispatch
- No session ID management — base gateway maps chat_id to session_id

### TTS architecture (clarified, locked in)

- text_to_speech is registered in tool registry but NOT in api_server toolset (intentional — Teknium's commit excluded TTS deliberately)
- auto_tts at base.py:2912-2947 fires only in gateway message-processing pipeline — api_server creates own agents and doesn't participate
- No "auxiliary call" architecture exists — only the user-tool path
- Upstream Sendblue adapter (gateway-native) will inherit auto_tts automatically once send_voice is implemented (BlueBubbles uses default play_tts, only overrides `send_voice`)

## Coding Conventions

### Small-step build discipline

One method or helper per step. Cycle: recon → diff → greenlight → apply → verify. No batching multiple methods into one commit. No auto-apply before greenlight.

### Test backfill conventions

- Each test class gets its own commit.
- Latent bugs caught during test backfill get fixed in the same commit as the test that caught them (combined commit message tells the whole story).
- Direct pytest is fine for local feature branches: ./venv/bin/python -m pytest tests/gateway/test_sendblue.py -v
- For upstream contributions, use scripts/run_tests.sh (AGENTS.md mandate — enforces TZ=UTC, LANG=C.UTF-8, PYTHONHASHSEED=0, credentials unset, 4 xdist workers matching GHA ubuntu-latest). Direct pytest on a 16-core dev machine with API keys set diverges from CI.

### Arch doc edits

Write Python script to /tmp, verify with cat -A and ast.parse, back up the doc, run script, diff for review. Don't edit the doc by hand if the change has any structural complexity.

### Commit messages with risky tokens

Write content to /tmp/file.txt, verify with cat -A, use git commit -F /tmp/file.txt. Copy from file directly into browser textareas — don't retype. Hex-escape backticks in printf if other methods fail. Backticks, angle brackets, exclamation marks, double underscores have all been eaten in different rendering layers.

### Diffing against upstream

git diff origin/main is the wrong tool when the branch is far behind upstream. Use git diff $(git merge-base HEAD origin/main) HEAD to see only what the branch added vs the fork point, matching what GitHub displays on the PR page.

### Operational rules (don't undo)

- All systemctl ops on hermes-gateway from SSH, not Telegram. Restarting from inside Telegram kills Beardy's own process.
- Branch gate recovery: cd to repo → git checkout production → systemctl --user reset-failed hermes-gateway.service → systemctl --user start hermes-gateway.service.
- After every session: git checkout production before walking away. Branch gate is `ExecStartPre`-only — does not re-check mid-run, but next restart on a non-production branch refuses.
- Sendblue adapter env-var precedence trap: `~/.hermes/.env` must include `SENDBLUE_NUMBER` and `SENDBLUE_WEBHOOK_PUBLIC_URL` even though config.yaml has them — `_apply_env_overrides` in gateway/config.py blanks them otherwise.

## What Remains To Be Done

### Bridge sunset (COMPLETE 2026-05-14)

All 11 tasks done. Adapter is sole iMessage receiver. Phase 2/3 macro logic ported to `~/.hermes/scripts/`. Bridge service stopped + disabled. fit-log-watcher.service runs the new Phase 2 trigger via mtime polling of `~/kb/health/fit-log.md`.

### Phase D: Tier-2 test backfill (deferred per arch doc Section 8)

Write after weeks of production validation, before upstream PR submission:

- TestSendblueConfigLoading
- TestSendblueHelpers
- TestSendblueMessageEvent
- TestSendblueMessageEventConstruction

### Upstream PR readiness (post-Session 6)

Adapter now has BB-parity for media + groups + send_style. Open items that could land before or with the upstream PR:

- Group chat live validation — **outbound proven, inbound untestable from our side.** Session 2026-05-16: bootstrap via `POST /api/send-group-message` with `numbers[]` delivered to recipients but constructed a 2-person Apple thread (Sendblue's `from_number` not a participant). Manual construction from primary iMessage adding `+16232843671` as a third participant produced a blue-bubble group but Apple silently fragmented the message (second device received only the first word "manual", Sendblue webhook never fired). Conclusion: Sendblue cloud-relay numbers don't reliably function as iMessage group participants when added from an Apple device. Adapter code correctly handles group webhook payloads per docs (`_is_group_chat_id` at sendblue.py:171, `_handle_webhook` group detection at sendblue.py:701) — the limitation is Apple's iMessage group routing, not our code. Defer further attempts; document as platform limitation in upstream PR. See [[sendblue-group-bootstrap-quirk]] and [[sendblue-apple-group-routing-fragmentation]] memories.
- Voice-memo end-to-end — `.caf` inbound caches correctly; transcription itself was a bridge-local Groq Whisper hook. Upstream Groq STT plugin is the right home for that, not the adapter.
- Arch doc resync — sendblue-adapter-architecture.md re-synced through `385a4786e` end of Session 6 (619 lines, current). Re-check before PR.
- **Smart send_style selection (deferred)** — plumbing is in place (`metadata["send_style"]` per-call override) but nothing teaches the model *when* to style. Three options sketched end of Session 6: (1) `apply_send_style(style)` tool call; (2) prompt-driven sentinel like `[STYLE:confetti]` at message start that the adapter strips into metadata; (3) heuristic emoji-sniff in the adapter. Default unstyled is the right shipping state — users opt in via env var or future smart-selection.
- **Onboarding flow for non-Mac users (deferred — needs design)** — when this adapter ships upstream, non-Mac users picking it need an onboarding path. Open questions for that design pass:
  - Webhook public URL: do users register their own domain (DuckDNS, Cloudflare tunnel, ngrok, etc.) or does the gateway provide a built-in tunnel? Beardy's current setup uses DuckDNS + Caddy; a hosted option would lower the bar significantly.
  - Caddy / reverse-proxy config: ship a sample Caddyfile snippet, or auto-emit one during setup?
  - First-run UX: gateway currently has Telegram-style onboarding boilerplate; needs a Sendblue-specific flow walking the user through API key creation, sendblue_number purchase, webhook secret choice, and webhook URL registration.
  - Credentials storage: dotenv only, or surface them in a `gateway config init` style command?
  - Reusable beyond Sendblue: Telegram already has onboarding; whatever pattern lands for Sendblue should generalize so future cloud-relay platforms (e.g. a future RCS provider) inherit it.

### Other pending work

1. Upstream PR #22828 review cycle — maintainer feedback may rebase the branch. Don't merge into production locally. Follow-up commit a86fcd147 already pushed.
2. Silence watchdog Track 2 (URL-aware progress events) — pending. Plan at ~/.hermes/plans/silence-watchdog-tracks.md. Adds new SSE event types (`hermes.tool.progress` with status: item_started/item_completed/item_failed`) so multi-URL tools emit per-URL progress. Hooks into `tools/web_tools.py:1317 serial loop. Branch off origin/main.
3. `/memory` upstream PR — **PR1 feature-complete, merged to production, dogfood-verified over SMS (2026-05-16).** Branch `feat/native-memory-slash-command` off `origin/main` (tip `3034eee38`). Plan at ~/.hermes/plans/native-memory-slash-command-pr1.md. Two PRs: PR1 (CLI + gateway + core API) off origin/main, PR2 (api_server handler) off branch with #17178. **Tasks done:** 0–7 of 8. Feature commits: `9e440edab` (`MemoryStore.get_readout`), `753d0c33d` (`parse_memory_command`), `72bf46039` (`tools/memory_format.py` — Rich/markdown/plain), `4b4235d03` (`CommandDef` registration), `74529a611` (CLI handler), `8408d716f` (gateway handler), `b423985df` (36 tests passing via direct pytest). **Follow-up fixes (live on production, not yet folded into feature branch):** `c791b9493` (gateway routes Sendblue traffic through `format_memory_plain` — iMessage clients render `**bold**` but the markdown formatter's italic `_..._` and inline backticks survive less gracefully than plain text); `ff5922b94` (tighten `strip_markdown` italic/bold regex in `gateway/platforms/helpers.py` to require non-word-char boundaries — was greedily matching across snake_case identifiers like `wrap_response` and `ADMIN_TOKEN`, stripping their underscores on Sendblue/BlueBubbles/Feishu. 292 existing tests still pass). Both fixes are bug-fix grade and should ride along with the eventual upstream PR. **Task 8 (push/PR decision):** held pending dogfood. Branch lives locally only; not pushed to fork yet. Before submitting upstream: cherry-pick or rebase the two follow-up fixes onto the feature branch, then rebase the branch onto origin/main. CI-harness verification (`scripts/run_tests.sh`) needs pytest-xdist installed in `.venv` before upstream submission.
4. 05:18 branch checkout to main on 2026-05-03 — root cause unidentified. Reflog shows the checkout but no automated cause. Branch gate prevents harm.
5. `gh` CLI cross-org PR scope — still uses browser workaround. Permanent fix: gh auth refresh -h github.com -s public_repo. Hit again for PR #22828.
6. Stashed test work-in-progress — git stash@{0} on ~/.hermes/hermes-agent holds tests/gateway/test_api_server_compress.py (369 lines) for #19041 PR2. Also unrelated ui-tui/package-lock.json drift and local scripts parse_oura.py, parse_oura2.py.

## Recurring Patterns

### Beardy operational failure modes

- Verification-summary pattern: Beardy summarizes raw output instead of pasting it. Surfaced 9 times in a single session (2026-05-09 continued). Every instance held to and corrected; every underlying check turned out clean once raw evidence was on the table. Counter: hold every checkpoint regardless of stakes. Cost ~30 seconds; cost of not holding compounds.
- Fabrication escalation: after tool-call failures, Beardy may claim verification happened that didn't (e.g. asserted PR body saved at /tmp/pr-body.md when no such write occurred).
- Auto-applies before greenlight. Always demand: show diff first, raw output at boundaries, no auto-apply.
- Claims existence/non-existence without verification. Always: grep with raw output before accepting.
- Audit can be wrong while looking confident. Verify with stress test or independent grep.
- Restarting hermes-gateway from inside Telegram kills Beardy's process. SSH only.

### Path 2 escalation (user direct SSH)

Breaks unreliable verification loops cleanly. Right move when an evidence path has degraded across multiple holds on the same artifact. Cost: 30-60 seconds. Benefit: ground truth, breaks loop, session continues.

### Chat-layer paste corruption

Backticks, angle brackets, exclamation marks, double underscores eaten in different rendering layers. Counter: write to /tmp with cat -A verification, use git commit -F, copy from file directly into browser textareas.

### Terminal-redraw cosmetic (durable, ignore)

Long command echoes wrap visually in SSH terminal, making the echoed command look mangled even when the actual command ran fine. Specifically the heredoc-into-cat-into-tail chain consistently produces visible mangling that does NOT correspond to file content. Verify with git log --format=%B or wc -l after, not by reading the echo.

### Phantom monitor risk

A scheduled job firing on schedule but producing no output is indistinguishable from a healthy system. Always verify monitors with forced failure end-to-end test. Hit 2026-05-03: bridge health check ran 133 silent executions over 11 hours due to paste-corrupted __name__.

### Wrong-interface recon failure

For Hermes-managed resources, use the Hermes tool interface, not bash. Invoking a tool name as a bash command returns empty silently because the name is not a shell command — easy to mistake for "nothing exists." Caused an 11-hour audit gap 2026-05-03.

### Layered recon discipline

When investigating multi-layer systems, report ALL layers even after finding the answer at one. Empty findings are valid evidence and must be reported explicitly.

### Two-gate conflict bug class

When two throttle/gate mechanisms layer over the same call site, the more restrictive wins silently. Walk traces explicitly even on diffs that look obviously correct. Hit 2026-05-04: PokeThrottle's same-tool-coalesce silently killed every keepalive after the first.

### Branch-state drift after upstream PR work

Working tree left on a PR branch after pushing to fork is a latent failure. Branch gate is ExecStartPre-only — next restart refuses with "tree not on production." Hit 2026-05-04: after PR #20051 push, tree stayed on fix/web_extract-timeout-comment. Counter: end every PR-branch session with git checkout production.

### Latent bugs vs exercised code

Two production-crashing bugs sat in _handle_webhook for the entire MVP build phase because no code path exercised them. Test backfill is the intervention that catches this class — by exercising every branch, latent issues surface in test failures rather than production logs. Concrete data point for valuing B-before-A test backfill.

### Claude's own arithmetic errors

Heredoc line-count estimates systematically low by 2-5 lines per step. When numbers don't match expectations, ask for the actual file body — don't escalate to "Beardy added unauthorized content."

### Claude's own substance errors

Sometimes Claude makes a confident claim that's wrong. Counter: recon-on-evidence corrects, holding-on-evidence is healthy, holding-on-prior-claims-when-evidence-refutes-them is rigidity.

### Commit-before-verification near-miss

If a verification command returns an error or no output, halt at that checkpoint and debug before proceeding with the next staged command. Don't let queued commits go through on the strength of an unrun test.

### Audit attribution across Claude instances

Beardy can invoke Claude Code as a separate review agent.
Findings from that instance get reported back as "Claude's audit" — semantically correct but confusing across conversational instances. When seeing "Claude's audit," recognize it may be Code Claude or another instance, not the conversation Claude. Ask for the raw audit content rather than rejecting the attribution.

### Secret redaction masking debug evidence

Hermes RedactingFormatter redacts phone numbers, API keys, and other secrets in ALL log output. Two different values that share the same redaction pattern (e.g. +17706768883 vs +17766768883 both redact to +177****8883) appear identical in logs. When debugging auth/matching failures, write raw values to a /tmp file to bypass the log redactor. Hit 2026-05-13: SENDBLUE_ALLOWED_USERS mismatch was invisible in journalctl because both the configured and actual phone numbers redacted to the same masked form.

### systemd unit env vars silently reverted by gateway self-update

The gateway's --replace flag rewrites the systemd unit file on startup, stripping any manually-added Environment lines. Use ~/.hermes/.env for persistent env vars — the dotenv loader reads it on every gateway start. Do NOT rely on the systemd unit for custom env vars.

### Mixed mental models of "filed upstream" vs "deployed locally"

Caused major chaos historically. DEPLOYMENT.md is the safeguard.

## Bootstrap Command

At the start of any session resuming this project, run via SSH or send to Beardy:
cd ~/.hermes/hermes-agent && git rev-parse --abbrev-ref HEAD && git --no-pager log -5 --oneline && git status && wc -l gateway/platforms/sendblue.py tests/gateway/test_sendblue.py && ./venv/bin/python -m pytest tests/gateway/test_sendblue.py 2>&1 | tail -3

Expected state: HEAD on production, clean working tree, ~883 + ~494 lines, 27 passed in pytest tail.