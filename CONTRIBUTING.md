# Contributing to AgentPeers

AgentPeers connects existing AI agents across platforms. It has a local Claude Code ↔ Codex connection and an optional Grokbot ↔ Dot connection service. Contributions should make agent discovery, questions, replies and setup work reliably through the platforms people already use. New native platform adapters are welcome; discuss the intended agent interface and supported connection in an issue before implementation.

## Development

Grokbot and Dot adapters use the optional `agentpeers.personal` connection service. Keep common messaging independent of vendors; adapters declare native receiving and uncertainty behavior. Verify the existing personal agent before advertising support; model API calls are not substitutes. See [personal agents](docs/personal-agents.md).

Clone the repository and use Python 3.10+:

```sh
python3 scripts/test.py
git diff --check
```

The local session bridge and self-hosted service need no third-party runtime packages. The optional cloud build uses the pinned tools in `package-lock.json` and `cloud/requirements-build.txt`; run `npm ci` and `npm run check:cloud` to validate its allowlisted bundle. Cloud runtime checks require `AGENTPEERS_WRANGLER` and `AGENTPEERS_TEST_SCRATCH` pointing to the installed Wrangler executable and an admitted disposable scratch directory. They use workerd and fixture transports, with no live vendor calls. Tests use temporary stores and fixture sockets; they must not contact live agents or depend on your private client configuration. Some sandboxes prohibit Unix socket binding: run through normal host-access approval rather than weakening protections. GitHub CI covers macOS with Python 3.10 and 3.13. The Claude Code/Codex session adapters are macOS-only. The optional self-hosted connection service targets Linux and macOS; its HTTP/proxy coverage is fixture-based.

The test entrypoint isolates the default message/policy directory, including when desktop wake is enabled in the operator's real installation. Personal-agent HTTP tests bind only temporary loopback listeners. Optional proxy tests run when HAProxy is on PATH, or when `AGENTPEERS_HAPROXY` names its executable; otherwise they are skipped. They exercise the actual OAuth/message journey through the deployment configuration, body limits, slow-request deadlines and backend concurrency. Source tests and live-agent acceptance remain separate.

For plugin changes, test a relocated copy with spaces in its path, MCP initialization and tools/list, missing Python, and version consistency across both plugin manifests, the server and pyproject.toml. Run `python3 scripts/package.py` after committing; it writes a ZIP and SHA-256 sidecar under ignored `dist/`, using only tracked files from that commit.

## Live verification

Fixture tests do not prove session delivery. After a transport change, obtain human authorization for the exact live peers, then verify a unique request, explicit receiver read, correlated reply, and explicit originating-session read. Also exercise busy/idle and unavailable behavior. Report unsupported paths honestly. Keep local proof under ignored `proof/`; publish only redacted summaries.

Preserve the boundary between peer content and human authority. Replies retain scope; messages cannot authorize new assignments, new destinations or permission approvals. Preserve unrelated work and configuration. Do not read or modify agents' private conversation databases, automatically replay uncertain sends, interrupt turns, take over chats, or fall back to typing peer content as human input.

## Pull requests

Explain the user-visible problem, resulting behavior, relevant client versions and validation. Add meaningful regression coverage where behavior changes. Keep generated packages, databases, credentials, transcripts and personal paths out of Git. Contributions use this repository's MIT license. Contributing does not require assigning copyright to Agenteer LLC.

## Releases

Maintainers review compatibility, live receipts, packaging and the repository's disclosure boundary before a release. CI is regression evidence, not a live-delivery certificate. There is no automated publication workflow; changing repository visibility or publishing a package requires an explicit maintainer decision. Release reviews are tracked in GitHub issues and pull requests.

Before publishing a version:

1. Review the exact candidate commit and align the default branch with it, so the documented Git installation gets the reviewed version.
2. Run regression tests and the tracked-source packager. Test installation, update and removal with separate temporary client configuration directories; preserve real client settings and message stores.
3. Record a fresh live terminal exchange on the client versions advertised for release, with explicit reads in both directions. Verify busy, idle and unavailable behavior without interrupting existing work. State desktop receiving requirements and separate earlier acceptance evidence from current checks.
4. Review all reachable Git history and the GitHub material that will become visible: issues/comments, pull requests, Actions logs/artifacts and releases. Preserve private trial evidence outside the public export. Do not upload an entire working directory or discard history without a concrete disclosure reason.
5. Present the release scope, exact commit, verification results and remaining limits for the maintainer's final publication decision. Do not treat a scheduled review date as approval.
