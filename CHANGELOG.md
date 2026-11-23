# Changelog

## Unreleased

- Report when an unverified named send saved nothing for delivery and show the full session ID if complete local discovery found one exact match. When desktop IPC is refused or missing, save a named reply unread to the original Codex sender's exact mailbox if it is the sole match in complete local discovery. The receipt preserves the transport failure. The existing opt-in wake rules apply.

## 0.11.1 — hold no-reply messages without waking

- New messages with `expects=none` to a Codex chat with wake on and no live route stay unread without queuing a wake. The receipt says `saved unread; not woken: no reply expected`. Replies still queue a wake regardless of `expects`. The next inbox read collects held messages, including when a later message queues a wake.

## 0.11.0 — wake a Codex chat in ChatGPT.app

- Opt-in per chat: `agentpeers wake on|off <chat>`, `agentpeers wake off --all`, `agentpeers wake status`. Only the person in a terminal, or the chat itself, can turn it on; the command refuses inside a Claude Code session or another Codex chat. Who turned it on or off, and when, is recorded. `AGENTPEERS_WAKE=off` overrides everything, including desktop wake.
- A send to a Codex chat with wake on and no live route stores the message as before, then queues one constant wake signal with the Codex CLI bundled in the app (`codex queue --thread <id>`) and opens `codex://threads/wake/<id>` in the background. The receipt says `saved unread; wake queued`, never delivered. A failed or uncertain wake is recorded and never retried. Live routes keep priority.
- The wake text is one constant pinned byte for byte by a test. No sender, body, summary or scope ever goes into it; the message is read only through the inbox tool.
- No wake for a chat that anything outside the desktop app owns: every process holding the chat's rollout file or naming the chat on its command line must trace back to the app's own launchd-started process. A terminal or embedded server, an archived, missing or duplicated rollout, or any unreadable answer refuses the wake. The check is the only gate and runs immediately before the queue; nothing is checked after it, because a queued signal may already have run. Codex has no lock an outside tool can hold, so a process that starts and loads the chat between the check and the queue is not excluded; the receipt records both times.
- Coalescing: no new wake while an earlier one for that chat has not yet collected its mailbox (no complete inbox read since, and some covered message still unread; at most one hour). Sends and inbox reads are ordered by one write transaction each. A wake still being queued is reported as "wake not yet confirmed", not queued.
- Inbox always returns every unread message, with no cap, alongside the 100 newest, so one read collects the whole mailbox.
- `inbox --unread` (MCP `unread: true`). Every inbox and acknowledge row now carries the same authority note as a live envelope.
- `list_sessions` and `list` show wake on or off for each Codex chat. Setup reports it, and the docs and skill disclose what the chat shows: a user-looking message from AgentPeers, the chat's reply and a turn-complete notification.

## 0.10.1 — Codex desktop inside ChatGPT.app

- Codex desktop 26.1117.31558 moved into ChatGPT.app (same bundle, `com.openai.codex`). The inspected build ships no desktop IPC router, and its leftover socket refuses connections. On that build a refused or missing socket now fails with `desktop_ipc_unsupported`: the observed connection error, the app and version, and the statement that live delivery was not attempted. A saved message is reported as unread in the recipient's mailbox. A newer build with the same failure keeps `desktop_connection_unavailable` and says its IPC is unverified.
- Guidance to move a chat into a terminal on the shared server first says to stop the chat's active session in the app, then to confirm the app has released it (no app process holds its rollout file), and not to resume it in a terminal if that cannot be confirmed.
- Session listings and setup report the same finding; setup reads the Codex desktop version from either app name and skips the separate ChatGPT chat app.
- Codex.app builds with desktop IPC keep their existing behavior. The shared-server terminal route is unchanged and was verified live against Codex CLI 0.169.0.

## 0.10.0 — research can ask its local Codex launcher

- Add temporary, scoped research handoffs pairing an ordinary ChatGPT requester with the exact local Codex session that launched it. Reuse AgentPeers Context’s separate factual requester identity and the existing cloud service.
- Start a finite local relay for native delivery, explicit consumption, sourced answers and requester retrieval. Preserve uncertain outcomes without automatic resend.
- Reject new messages to generic local-machine peers before saving them; replies to existing local-originated conversations remain supported.
- Verify a real Pro → exact local Codex launcher → Pro exchange, including native delivery while the receiver is active, both agents’ consumption, and closure of the temporary relay.


## 0.9.1 — clearer addresses and receiving setup

- Reject shortened or invalid `codex:` and `claude:` session IDs before discovery or message creation, with a structured address correction instead of a misleading receiver failure. Full IDs and ordinary session names remain supported.
- Explain that local discovery may omit cloud-hosted Codex coordinators; the direct Dot route reaches Dot, not a separate Codex chat.
- Document the Dot listener’s required approval of a new local connector, explicit refusals for rejected questions, and the recovered live Claude Code exchange.

## 0.9.0 — local sessions can ask Dot directly

- Optional direct connection from Claude Code and Codex sessions to the owner's existing cloud service; no helper Codex chat or desktop receiving owner is needed.
- Separate local connector credential and originating-session reply binding. Existing Dot/Grokbot credentials and conversations stay intact.
- Bounded reply retrieval through MCP tools and CLI, with explicit read receipts and no automatic send retries. Idle or closed local sessions are not automatically woken.

## 0.8.1 — local connection profiles

- Add optional private connection settings for custom Claude Code/Codex profiles and a shared mailbox when a client filters MCP environment variables. Ordinary installations retain their defaults and existing messages.
- Validate profile configuration before session discovery; reject conflicting settings, unsafe files and concurrent attempts to replace another connection.
- Preserve Claude’s normal registration lookup when only the mailbox changes, while respecting explicitly selected profiles.
- Clarify local Codex receiving requirements, saved-message recovery and the limits of desktop wake. Document an observed Codex 0.164.0 terminal approval recovery without changing permission policy.

## 0.8.0 — cloud source preview

- Add a separate ordinary ChatGPT → Dot factual-context connection, with discoverable owner-approved topics and sources, per-chat permission and explicit answer retrieval.
- Add a normal owner setup page with a 15-minute sign-in, readable pairing review and individual revocation. Preserve existing Dot–Grokbot messaging.
- Keep browser form submissions on the correct origin and offer same-site continuation for an existing owner sign-in, preserving the strict cookie and origin checks.
- Accept sourced answers, unavailable and out-of-scope results; preserve fixed deadlines, duplicate suppression and exact receiver binding.
- Verify a real sourced answer and an unrelated-question refusal retrieved in the originating ChatGPT chat, alongside the existing Dot–Grokbot round trip.
- Keep named Codex context delivery separate; its live adapter is not implemented.

## 0.7.0 — cloud source preview

- Add the optional AgentPeers connection service for Grokbot ↔ Dot, alongside the existing local Claude Code ↔ Codex connection. First native adapters: Grokbot routines and Dot MCP Events.
- Keep personal messages, OAuth grants and subscriptions in a separate private store; preserve local Claude Code/Codex session delivery.
- Add peer-bound credentials, owner-approved connections, explicit consumption, atomic processing claims, bounded notifications and uncertainty-preserving recovery.
- Add a standard optional proxy recipe with bounded connections, body size and request deadlines; verify it with real loopback proxy traffic.
- Present AgentPeers as cross-platform communication between existing agents, with separate setup paths, scoped plugin listings and discoverable service CLI help.
- Add cloud runtime, local contract and loopback HTTP tests. Live Grokbot ↔ Dot checks verified both initiators, replies and a follow-up from each agent. Muse and Duet are not yet implemented.

## 0.6.2

- Validate MCP arguments before opening a mailbox or attempting delivery. Missing or invalid fields return a structured correction with explicit confirmation that no message was created or submitted.
- Make the authorization field explain that agents should summarize permission already given, without asking for duplicate approval or inventing authority.
- Retry one transient Codex session-discovery timeout in a fresh read-only process, with bounded initialization and pagination. Preserve other runtime results and report persistent lookup failure as unknown receiving state.
- Keep message submission and uncertain delivery outside automatic retry. A corrected call can reuse its message ID without duplicate sends.

## 0.6.1

- Add explicit desktop wake opt-in: open an unloaded Codex destination using its native app link, verify its receiver, then deliver peer tool output. The app may select that chat.
- Keep discovery read-only and preserve message provenance, exact destination IDs and no automatic replay after uncertainty. No new background service or terminal takeover.
- Allow unique unloaded names when desktop wake is enabled; refuse eligible duplicate names.
- Let normal desktop discovery finish before treating its missing-owner response as a timeout.

## 0.6.0 — first public release candidate

- Introduce agentpeers: local Claude Code terminal ↔ Codex terminal discovery, scoped messages, correlated replies and explicit read receipts.
- Connect Codex terminal or desktop sessions with Claude Code terminal sessions; document the tested versions and distinguish a running app from a compatible receiving chat.
- Support exact session names, deliberate named replacements, pinned session IDs, safe file/stdin input, response expectations and one blocking question from an explicitly assigned worker.
- Preserve uncertain delivery outcomes without automatic replay. Expose saved mailbox replies and sender return-route warnings.
- Package native plugins and guided setup for both clients with Python 3.10+ and no third-party Python runtime dependencies.
- Retain existing external message stores and environment settings when upgrading from Peerbridge.

Cross-machine transport and automatic wake-up of closed/unloaded chats are outside this release.
