# Ask Dot for factual context from ChatGPT

Use an ordinary ChatGPT chat to ask your existing Dot about a project or topic it knows. Dot returns a short answer with its sources and observation times. You choose what it may share; the requesting chat gets no access to Dot's inbox or general messaging tools.

![Factual-context architecture](assets/context-requests.svg)

**Development candidate — live acceptance pending.** The earlier synthetic exchange verified the route. The usable topic, source and setup flow described here is implemented locally and is being verified before release. Named Codex context routing is not implemented.

This uses the same AgentPeers service as the [Grokbot ↔ Dot connection](personal-agents.md), with a separate ChatGPT connection and separate permissions. Keep the existing Dot receiver and Grokbot routine. No new model API key, server or scheduled polling job is needed. ChatGPT must support custom MCP apps in the account and model being used; the feature is independent of model choice.

## 1. Choose what Dot may share

Upgrade your existing AgentPeers service to a version containing this feature. Open `https://YOUR-SERVICE/context` and sign in with its existing AgentPeers password. The owner sign-in lasts 15 minutes so you can finish setup without repeatedly entering the password. It is not given to ChatGPT or Dot.

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
| `context_destinations` | Discover the configured Dot topics and their allowed factual scope. |
| `request_context` | Submit one stable question after the owner approves this chat and topic. |
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

Disconnecting revokes only factual-context credentials and chat permissions. Normal Dot–Grokbot grants, receiving subscriptions, messages and the local Claude Code/Codex mailbox remain separate. Records are retained for inspection. The old closed synthetic-validation grant remains closed and is not reused by this feature.

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
