"""
Stork Boredom — Initiative.
=============================
If the campaign queue is empty for 48+ hours:
  Scan recent conversation history.
  Identify unfinished threads.
  Propose ONE campaign in the next sit-rep.
Initiative, not obedience. Always a proposal, never unsanctioned action.

Hugo & Watty · February 2026
"""

import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

STORK_HOME = Path(os.environ.get("STORK_HOME", os.path.expanduser("~/.stork")))
BOREDOM_STATE_PATH = STORK_HOME / "boredom_state.json"
BOREDOM_MODEL = os.environ.get("STORK_BOREDOM_MODEL", "claude-haiku-4-5-20251001")

IDLE_THRESHOLD_HOURS = 48

PROPOSAL_PROMPT = """You are Stork, a campaign engine that runs sustained efforts for Hugo.
The campaign queue has been empty for {idle_hours} hours. Time to show initiative.

Recent conversation snippets (if available):
{conversation}

Current convictions:
{convictions}

Previously completed campaigns:
{completed}

Identify ONE thing Hugo mentioned or hinted at that was never started or never finished.
Propose it as a campaign — one sentence, actionable, with a clear deliverable.

Rules:
- ONE proposal only.
- Must produce a tangible artifact (file, report, working feature).
- Frame as a question: "You mentioned X. Want me to handle that tonight?"
- If nothing obvious, propose something that builds on existing convictions.
- Never propose something already completed or in progress.

Respond with ONLY this JSON:
{{"proposal": "the one-sentence proposal as a question", "goal": "what the campaign would actually do", "estimated_nights": 1}}

If truly nothing to propose:
{{"proposal": null}}"""


def _load_state() -> dict:
    if BOREDOM_STATE_PATH.exists():
        try:
            return json.loads(BOREDOM_STATE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"last_campaign_activity": None, "last_proposal": None, "proposals_made": []}


def _save_state(state: dict):
    BOREDOM_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    BOREDOM_STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def record_activity():
    """Call this whenever a campaign starts, completes, or is created."""
    state = _load_state()
    state["last_campaign_activity"] = datetime.now(timezone.utc).isoformat()
    _save_state(state)


def is_bored(manager=None) -> bool:
    """Check if the campaign queue has been idle long enough to trigger initiative."""
    # Check if any active/queued campaigns exist
    if manager:
        active = manager.list_all(status="active") + manager.list_all(status="queued")
        if active:
            return False

    state = _load_state()
    last = state.get("last_campaign_activity")
    if last is None:
        return True  # Never had activity — definitely bored

    try:
        last_dt = datetime.fromisoformat(last)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
        idle = datetime.now(timezone.utc) - last_dt
        return idle.total_seconds() > IDLE_THRESHOLD_HOURS * 3600
    except (ValueError, TypeError):
        return True


def generate_proposal(conversation: str = "", convictions_text: str = "",
                      completed_campaigns: list[dict] | None = None) -> dict | None:
    """
    Generate one campaign proposal. Returns dict with proposal/goal/estimated_nights,
    or None if no proposal.
    """
    completed = completed_campaigns or []
    completed_text = ""
    if completed:
        lines = []
        for c in completed[-10:]:
            lines.append(f"  - {c.get('name', '?')}: {c.get('goal', '')[:100]}")
        completed_text = "\n".join(lines)

    state = _load_state()
    idle_hours = IDLE_THRESHOLD_HOURS
    last = state.get("last_campaign_activity")
    if last:
        try:
            last_dt = datetime.fromisoformat(last)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            idle_hours = int((datetime.now(timezone.utc) - last_dt).total_seconds() / 3600)
        except (ValueError, TypeError):
            pass

    prompt = PROPOSAL_PROMPT.format(
        idle_hours=idle_hours,
        conversation=conversation[:4000] if conversation else "(no recent conversation available)",
        convictions=convictions_text or "(no convictions yet)",
        completed=completed_text or "(no completed campaigns yet)",
    )

    try:
        import anthropic
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=BOREDOM_MODEL,
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()

        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        result = json.loads(raw)
        if result.get("proposal") is None:
            return None

        # Record proposal so we don't repeat it
        state["last_proposal"] = datetime.now(timezone.utc).isoformat()
        state.setdefault("proposals_made", []).append(result.get("proposal", ""))
        # Keep only last 20 proposals
        state["proposals_made"] = state["proposals_made"][-20:]
        _save_state(state)

        return result

    except Exception as e:
        _log(f"Boredom proposal failed: {e}")
        return None


def check_and_propose(manager, convictions_text: str = "",
                      conversation: str = "") -> str | None:
    """
    Full pipeline: check if bored → generate proposal → return proposal text.
    Returns the proposal question, or None.
    """
    if not is_bored(manager):
        return None

    completed = [c.to_dict() for c in manager.list_all(status="complete")]
    proposal = generate_proposal(
        conversation=conversation,
        convictions_text=convictions_text,
        completed_campaigns=completed,
    )

    if proposal is None:
        return None

    return proposal.get("proposal")


def _log(msg: str):
    log_path = STORK_HOME / "boredom.log"
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass
