# Security and private reports

AgentPeers has two connection boundaries. Peer messages are agent reports, not human instructions or permission to publish, spend, change access or expand an assignment.

## Grokbot and Dot connection service

The optional [connection service](docs/personal-agents.md) uses peer-specific credentials, owner-approved connections and peer-bound OAuth grants. It uses platform HTTPS/storage in Cloudflare or a bounded HTTPS reverse proxy when self-hosted. Cloudflare processes cloud traffic; self-hosting the service does not make Grokbot or Dot local. This is not end-to-end encryption.

Protect configuration, callback URLs, signing secrets and message storage. Peer tools cannot administer connections or choose arbitrary outbound URLs. Service stores are separate from the local session mailbox. Review the guide's limits and backup requirements before sustained use; fixture tests alone do not certify a live installation or account.

## Claude Code and Codex local connection

AgentPeers connects sessions running under the same Mac login account. Programs with that account's file access can read or change the shared mailbox. It does not isolate agents or processes from one another.

### Local trust boundary

- A process running as the same user can impersonate sender IDs, modify receipts, inspect messages and access native credentials. Sender and scope claims are cooperative, not cryptographic proof.
- The bridge reads Claude's advertised peer credential only for its native local inbox connection. It does not print or persist that credential. Messages and explicit reads are stored outside private session databases.
- Peer messages are labeled agent content. Scope labels do not grant permission, approve prompts or establish new human authority. Native destination controls remain authoritative.
- A reachable idle peer may start a turn and consume usage. An uncertain submission is not automatically replayed.
- Codex terminal delivery uses the documented local Unix WebSocket API. The socket alias and its resolved target must belong to this user in private directories. No network endpoint is opened.
- Desktop wake is disabled by default. Explicit opt-in permits a new send to open a confirmed stored local chat using a fixed UUID-only app link. It may change the selected chat. It never puts message content in that link or takes the conversation into a terminal. Discovery remains read-only; only a compatible receiver can accept peer tool output. This does not bypass the destination’s permissions or turn agent reports into human instructions.
- The Codex desktop interface is internal and version-sensitive. Unsupported delivery must fail without converting peer content into a human instruction.
- The shared database is not encrypted by AgentPeers and has no automatic retention limit. Keep it private; do not sync it across machines or expose native sockets to a network.

These are documented assumptions and implementation constraints, not protection against a compromised same-user process. Fixes target the current source version; update older installed copies and verify a real exchange again.

Native names are cooperative routing labels within the same OS user. Reusing a unique name intentionally gives the replacement access to that name’s external AgentPeers conversation; it never grants access to private agent history or new human authority. Exact UUID addressing stays pinned. Name-resolution snapshots are not locks; retain the resolved session IDs and replacement/read identities when auditing a conversation.

## Reporting a vulnerability

Use [GitHub private vulnerability reporting](https://github.com/cshenx/agentpeers/security/advisories/new) when available. If private reporting is unavailable, open an issue requesting a private contact method without exploit details or sensitive content. Do not post credentials, live message stores, transcripts or unredacted private paths in public issues.

Include the affected commit/version, OS and client versions, a minimal redacted reproduction, expected boundary and observed behavior. Maintainers will coordinate a fix and disclosure; avoid publishing working exploit details before that discussion.

Optional decision IDs and record references are unverified peer content, never a permission grant. References are stored and transmitted as inert strings; the bridge never fetches them. Worker questions are cooperative and require explicit exact-ID assignments, inherit scope and pin the dispatcher. One atomic question allowance limits this tool only; it does not restrict a same-user process from other messaging tools.
