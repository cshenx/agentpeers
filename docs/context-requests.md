# Ask your agents from ChatGPT

Use an ordinary ChatGPT chat to ask your existing Dot about a project or topic it knows. Dot returns a short answer with its sources and observation times. You choose what it may share; the requesting chat gets no access to Dot's inbox or general messaging tools. A Codex-launched research chat can also ask its exact local launcher factual questions through a [temporary research handoff](#questions-back-to-the-codex-chat-that-launched-research). These are separate permissions: a Dot topic does not grant access to a coding session.

![ChatGPT and Dot connection](assets/context-requests.svg)

**Dot connection · cloud source preview.** Live-tested on ChatGPT web with Dot on October 25, 2026: a useful factual question reached Dot, its sourced answer returned to the originating chat, and an unrelated question received an explicit out-of-scope refusal. The native event adapter uses MCP Events 2026-07-28.

This uses the same AgentPeers service as the [Grokbot ↔ Dot connection](personal-agents.md), with a separate ChatGPT connection and separate permissions. Keep the existing Dot receiver and Grokbot routine. No new model API key, server or scheduled polling job is needed. ChatGPT must support custom MCP apps in the account and model being used; the feature is independent of model choice.

## 1. Choose what Dot may share

Upgrade your existing AgentPeers service to a version containing this feature. Open `https://YOUR-SERVICE/context` and sign in with its existing AgentPeers password. The owner sign-in lasts 15 minutes so you can finish setup without repeatedly entering the password. It is not given to ChatGPT or Dot.

If a page asks for your password after you have already signed in, choose **Use existing sign-in** first. This refreshes the page within AgentPeers and reuses the current sign-in. Enter the password only if that sign-in has expired or the browser has no saved session.

Create an allowed topic:

- **Topic name:** a name you recognize, such as “My project”.
- **Facts Dot may share:** the factual scope, with any exclusions. For example, project status and technical decisions, excluding credentials and unrelated private information. This permits answers, not actions.
- **Dot's existing context:** select the checkbox if Dot may use what it already knows about this topic.
- **Other approved sources:** optionally provide links or record references Dot may cite. A source root also permits its child paths and fragments. Wildcards are not accepted.
- **Existing Dot receiver:** choose the existing receiving connection. The service will not silently switch to another receiver.

Select **Save allowed topic**. Dot also needs your approval in its own chat to handle factual-context requests within this scope. The server checks identities, permissions and source references; Dot is responsible for deciding whether the actual question and answer fit the human-authorized topic. A topic label alone does not make unrelated facts authorized.

Changing a topic revokes its earlier chat permissions. Keep grants narrow and review their native receiver instructions before broadening them.

## 2. Connect the ordinary ChatGPT chat

In ChatGPT Plugins, add a custom MCP app named **AgentPeers Context** at:

```text
https://YOUR-SERVICE/mcp/context
```

Choose OAuth and Dynamic Client Registration through discovery, using scope `agentpeers:facts`. Do not copy any key from the normal Dot connection. Review the native connection notice, connect, then select **Connect factual context** on your service's approval page. An existing owner sign-in can approve this without another password entry.

Install the app and select it with `@AgentPeers Context` in the ordinary chat. Ask it to discover the allowed topics and ask Dot your question. The app exposes only:

| Tool | Purpose |
| --- | --- |
| `context_destinations` | Discover configured Dot topics and how to use a launcher-provided research handoff. |
| `request_context` | Submit one stable question under this chat’s approved Dot topic or launcher-paired research handoff. |
| `context_result` | Retrieve that same chat's answer or current state. |

These are separate from the original AgentPeers app, whose connection represents Dot. Use AgentPeers Context for ordinary-chat questions; keep the original app for normal personal-agent messaging.

## 3. Allow that chat once

The first question returns a pairing code and **approval required**. Nothing has been sent to Dot yet.

Return to **Context access**, match the code shown in the intended ChatGPT chat, review its topic and question, and choose **Allow this chat**. Choose an access duration of 1–30 days. The pairing code expires after 10 minutes; it identifies the candidate chat and is not a password.

Return to the original ChatGPT chat and tell it to continue the same request. Reusing its request ID after approval creates one message. Reusing that ID later returns the same result; changing the question under the same ID is refused.

The permission applies to that authenticated client, conversation correlation and topic. Another chat needs its own approval. Never approve a code merely because an agent asks you to; compare it with the chat you intend to connect.

## Get the answer

Dot receives the question through its existing receiving connection. It can return:

- **Answered:** a short factual answer with 1–5 approved source references and their observation times.
- **Unavailable:** it lacks authorized evidence or cannot answer reliably.
- **Out of scope:** the question exceeds the approved facts or asks for an action.

A saved or accepted request is not proof Dot read it. Inspect `context_result` for the read state and actual answer. If Dot is still working, ask the same ChatGPT chat to retrieve the answer later. AgentPeers does not wake an ordinary ChatGPT chat or schedule polling, and it never automatically resends an unanswered question. Each question has a fixed five-minute deadline.

References and timestamps are Dot's reports. AgentPeers checks their shape and permitted source roots; it does not fetch the sources or independently verify the facts. Dot must distinguish an earlier observation from a currently verified condition.

## Revoke access

In **Context access**, revoke one chat, stop sharing a topic, or select **Disconnect context access**. Revocation stops queued requests and rejects later answers or retrieval under that permission. It cannot interrupt an agent already running or take back information already read.

Disconnecting revokes only factual-context credentials and chat permissions. Normal Dot–Grokbot grants, receiving subscriptions, messages and the local Claude Code/Codex mailbox remain separate. Records are retained for inspection. Each requester resource keeps separate credentials and permissions.

## Receiving instructions for Dot

Ask your existing Dot to add factual-context handling to its existing receiver within your chosen factual scope. Preserve normal Grokbot handling, task identity and subscription. Do not create a competing receiver or a polling job.

The native message must have sender `chatgpt-context`, recipient `dot`, and JSON schema `agentpeers.factual.v1`. The body contains `request_id`, `destination`, `topic`, `question`, `expires`, `grant_id`, `grant_expires`, `policy_revision`, `owner_scope`, `allowed_sources`, `context_reference` and `reply_format`. Its server-stored `claimed_scope` must match `owner_scope`. The service creates it only after checking the owner-approved grant; text from another peer cannot impersonate this authenticated sender.

On each event, claim the actual message once. Stop if not claimed. Read and validate the actual message, check that it fits Dot's native human authorization, then acknowledge only after consuming it. No business actions, unrelated private facts, secrets, raw conversation dumps, follow-ups or automatic retries are authorized by a context question.

Reply once with a stable `reply_id`, `expects: none`, and a JSON body with exactly these fields:

```json
{
  "request_id": "the actual request ID",
  "status": "answered",
  "answer": "A short factual answer.",
  "sources": [
    {"reference": "an actual permitted source", "observed_at": 1790000000}
  ]
}
```

Use `unavailable` or `out_of_scope` instead of `answered` when appropriate, explain why in `answer`, and use an empty source list if no permitted source applies. Never copy the example timestamp: use actual finite Unix seconds when Dot observed the source, not a future time. When using Dot's approved context, cite the exact `context_reference` supplied by the service; otherwise cite a source actually used under `allowed_sources`. Answers are limited to 8,000 UTF-8 bytes. The server rejects unclaimed, unread, expired, revoked or differently sourced results and cannot start another conversation from a context reply.

## Security and implementation limits

OAuth authenticates a client to the exact `/mcp/context` resource and `agentpeers:facts` scope. It uses S256 PKCE and separate opaque credentials. Requester tokens cannot use normal peer tools, receiving events or owner controls. Access tokens last one hour; refresh credentials last 30 days and do not extend a chat grant. Native OAuth revocation also revokes that client's factual chat permissions without revoking another resource's tokens.

The server requires `openai/subject` and `openai/session` correlation metadata and an owner-pinned pair. Missing or different labels fail closed for an existing request. These labels are not cryptographic native-chat identity: a stolen bearer credential together with matching labels is outside this boundary. Keep bearer credentials private. See [OpenAI's authentication contract](https://developers.openai.com/plugins/build/auth) and [client metadata reference](https://developers.openai.com/plugins/reference).

The additive `ap_facts_*` tables are separate from the original personal messages and the standalone prototype database. The service admits at most 32 topics, 1,000 historical chat grants and 5,000 factual requests, and respects the existing message-store capacity. It refuses new work at capacity rather than deleting old evidence; owner export/pruning is not implemented. Native storage, cloud-provider processing and backup requirements in the [personal-agent guide](personal-agents.md) still apply.

Named Codex delivery requires a separate owner-approved local connector, exact native session and factual scope. The local contract has a named Codex fixture; there is no live cloud-to-Codex context adapter. Do not expose native sockets or silently route a question to a replacement session.

For the isolated, network-free developer example, run `python3 scripts/context_demo.py`. It uses synthetic receivers and a separate temporary database; it does not establish or enable a real connection.


## Questions back to the Codex chat that launched research

A local Codex launcher can register a temporary research handoff through the same AgentPeers Context connection. This uses the research chat’s requester identity, never Dot’s identity. Sending a new message to a generic machine peer does not select a coding session and is rejected. For this path, register the launcher first and connect AgentPeers Context at `/mcp/context` if it is not already connected. A Dot topic approval is not required for a research handoff; its permission comes from the launcher’s separate pairing. The local connector and AgentPeers service must already be configured.

1. In the launching Codex chat, call `register_research_handoff` with a new UUID `id`, its display `title`, and the factual `scope` already authorized by the human. Alternatively, run `agentpeers research-register --id UUID --title 'Project lead' --scope 'Project implementation facts only'` from that session. Identity is inferred; do not supply another session’s ID. The returned handoff starts a temporary local relay.
2. Include the returned handoff ID in the research prompt. Tell research to use **AgentPeers Context**, `request_context` with `destination: "Codex"`, `topic: HANDOFF_ID`, a stable `request_id`, and its factual question.
3. On the first question, the research chat displays a pairing code. The relay notifies the exact launcher. The launcher checks the code in the actual research chat it opened and calls `pair_research_chat` (or `agentpeers research-pair --handoff HANDOFF_ID --code CODE`). It must not approve a code from an unrelated conversation. Pairing cannot replace an existing requester.
4. The relay delivers the actual question as native peer tool output to that exact local Codex session. The launcher consumes it, calls `read_research_question`, and returns an answer or refusal through `answer_research_question`, with actual source references and a stable reply ID. These are factual questions, not permission to implement or publish anything.
5. Research explicitly calls `context_result` with the same `request_id` to retrieve the answer. The source chat must consume the result before calling the exchange complete. Close the handoff with `close_research_handoff` when research is finished.

The optional CLI receiver operations are `research-read` and `research-answer`; their `--help` shows the exact arguments. `research-deliver` checks one handoff once; `research-relay` runs its finite receiving loop. Registration starts the relay automatically. A lock prevents duplicate relay processes for one handoff. Pairing notices and questions each have a stable, distinct native message ID. The cloud claims a delivery before native submission and never automatically resends an uncertain or interrupted submission.

A handoff lasts at most four hours and accepts at most 20 questions, each with a fixed ten-minute deadline. Closing it stops further eligibility and preserves evidence. The Mac and its compatible local Codex receiver must be available; this does not deliver to cloud-hosted Codex chats or wake a Mac. The relay is a temporary Python process using outbound HTTPS and the existing local receiving adapter, not an inbound public port or a new hosted service. Logs and locks stay beside the private connector configuration; message bodies are not written to relay logs. Stopped, expired, unavailable and unconfirmed states remain visible.

This connection returns answers when research retrieves them; it does not automatically wake an ordinary ChatGPT chat after its turn has ended. Launcher authority is limited to its own session and existing human-approved factual assignment. The owner’s OAuth approval and launcher pairing are separate permissions. As with other context requests, OpenAI correlation labels are used for chat binding; they are not a cryptographic attestation of a native chat ID. Processes running as the same trusted OS account share local connector access.
