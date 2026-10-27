"""Resolve native names to a single reachable session; never reserve or resume one."""
import uuid
from . import native


def is_session_id(address):
    try:
        runtime, value = address.split(":", 1)
        uuid.UUID(value)
        return runtime in ("claude", "codex")
    except (ValueError, AttributeError):
        return False


def validate_destination(address):
    if not isinstance(address, str) or not address.strip() or len(address) > 2000:
        raise ValueError("destination must be an exact session ID or native name (1–2000 characters)")
    if address.startswith(("codex:", "claude:")) and not is_session_id(address):
        raise ValueError("Incomplete or invalid session ID. Use the full runtime:UUID from discovery "
                         "or an exact native session name; shortened IDs are not supported. "
                         "No message created or submitted. This is an address error, not a receiver outage. "
                         "The local session list can omit remote Codex chats; absence does not mean they ended.")


def inventory():
    rows = []
    for runtime, discover in [("claude", native.claude_sessions), ("codex", native.codex_sessions)]:
        try:
            rows.extend(discover())
        except Exception as exc:
            raise native.Unavailable("Cannot resolve names while " + runtime +
                                     " discovery is unavailable; use an exact session ID. " + str(exc)) from exc
    return rows


def native_name(row):
    # Discovery's display name can be a preview or PID, neither is a native name.
    return row.get("name_address")


def name_for(actor, rows=None):
    try:
        if rows is None:
            rows = native.claude_sessions() if actor.startswith("claude:") else native.codex_sessions()
        return next((native_name(r) for r in rows if r["id"] == actor), None)
    except Exception:
        return None


def resolve_name(name, rows=None):
    rows = inventory() if rows is None else rows
    candidates = {r["id"]: r for r in rows if native_name(r) == name}
    reachable = []
    failures = []
    for row in candidates.values():
        if row["runtime"] == "claude" or row.get("surface") == "shared-server":
            reachable.append(row)
        else:
            try:
                with native.Desktop() as client:
                    client.owner(row["id"].split(":", 1)[1])
                reachable.append(row)
            except (native.Unavailable, OSError, ValueError, KeyError) as exc:
                if (getattr(exc, "diagnostics", {}).get("code") == "desktop_owner_missing"
                        and native.desktop_wake_enabled()):
                    # Discovery stays read-only. Send will open the exact resolved
                    # chat. An unloaded duplicate is now also an eligible holder.
                    reachable.append(row)
                    continue
                failures.append({"session_id": row["id"], "reason": str(exc),
                                 **getattr(exc, "diagnostics", {})})
                continue
    unknown = [f for f in failures if f.get("code") != "desktop_owner_missing"]
    if not reachable or unknown:
        # A timed-out duplicate can still be live. Never select another holder
        # or turn a missing response into a claim that the session is offline.
        raise native.Unavailable("No unique receive route was verified for name " + repr(name) +
                                 "; no message submitted or queued. " +
                                 ("One or more matching receivers could not be checked; their session state is unknown. " if unknown else "") +
                                 "This does not prove a session ended or the app is closed. No automatic retry.",
                                 code="name_resolution_unverified" if unknown else "name_unreachable",
                                 candidates=failures, verified_candidates=[r["id"] for r in reachable])
    if len(reachable) != 1:
        choices = "; ".join(r["id"] + " (" + str(r.get("cwd") or "unknown directory") + ")"
                            for r in reachable)
        raise native.Unavailable("Ambiguous session name " + repr(name) + ": " + choices +
                                 ". Use an exact ID or give the sessions distinct names; nothing submitted.")
    return reachable[0], rows
