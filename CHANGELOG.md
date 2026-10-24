# Changelog

## 0.7.0 — unreleased candidate

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
