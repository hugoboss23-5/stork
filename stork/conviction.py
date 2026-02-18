"""
Stork Conviction — The belief system.
======================================
After every campaign (success OR failure), extract one conviction.
Same confidence math as metabolism.py: diminishing strengthen, scaled weaken,
50 cap, settled protection. Different prompts, different file.

Beliefs about HOW TO WORK, not about Hugo.
Failure convictions are more valuable than success convictions.

Hugo & Watty · February 2026
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

STORK_HOME = Path(os.environ.get("STORK_HOME", os.path.expanduser("~/.stork")))
CONVICTIONS_PATH = STORK_HOME / "convictions.json"
CONVICTION_MODEL = os.environ.get("STORK_CONVICTION_MODEL", "claude-haiku-4-5-20251001")

MAX_CONVICTIONS = 50
SETTLED_THRESHOLD = 0.7
MAX_SUMMARY_CHARS = 6000


# --- Confidence math (gospel from metabolism.py) ---

def _strengthen_amount(current: float) -> float:
    """Diminishing returns. Harder to move a strong conviction."""
    if current >= 0.9:
        return 0.04
    if current >= 0.8:
        return 0.06
    if current >= 0.7:
        return 0.08
    return 0.10


def _weaken_amount(current: float) -> float:
    """Scaled weakening. Strong convictions take bigger hits."""
    return 0.08 + current * 0.12


# --- Storage ---

def _empty_store() -> dict:
    return {
        "version": 1,
        "last_updated": None,
        "deltas_applied": 0,
        "convictions": [],
    }


def load_convictions() -> dict:
    if not CONVICTIONS_PATH.exists():
        return _empty_store()
    try:
        return json.loads(CONVICTIONS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _empty_store()


def save_convictions(store: dict):
    CONVICTIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONVICTIONS_PATH.write_text(json.dumps(store, indent=2), encoding="utf-8")


# --- Rendering for context injection ---

def format_convictions_for_context(store: dict | None = None) -> str:
    """Render convictions as natural language for injection into campaign planning."""
    if store is None:
        store = load_convictions()
    beliefs = store.get("convictions", [])
    if not beliefs:
        return ""

    lines = ["What Stork knows about how to work:"]
    sorted_beliefs = sorted(beliefs, key=lambda b: b.get("confidence", 0.5), reverse=True)

    for b in sorted_beliefs:
        conf = b.get("confidence", 0.5)
        text = b.get("belief", "")
        if not text:
            continue

        if conf >= 0.85:
            lines.append(f"  {text}")
        elif conf >= SETTLED_THRESHOLD:
            lines.append(f"  Learned: {text}")
        elif conf >= 0.5:
            lines.append(f"  Tentative: {text}")
        else:
            lines.append(f"  Weak signal: {text}")

    return "\n".join(lines)


def get_relevant_convictions(goal: str, store: dict | None = None) -> list[dict]:
    """Pull convictions relevant to a campaign goal. Simple keyword overlap for now."""
    if store is None:
        store = load_convictions()
    beliefs = store.get("convictions", [])
    if not beliefs or not goal:
        return beliefs  # return all if no goal to filter against

    goal_words = set(goal.lower().split())
    scored = []
    for b in beliefs:
        belief_words = set(b.get("belief", "").lower().split())
        overlap = len(goal_words & belief_words) / max(len(goal_words | belief_words), 1)
        # Always include high-confidence convictions (universal lessons)
        if overlap > 0.15 or b.get("confidence", 0) >= 0.85:
            scored.append((overlap + b.get("confidence", 0), b))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [b for _, b in scored[:10]]


# --- Delta application (mirrors metabolism.py exactly) ---

def _find_conviction(beliefs: list, target: str) -> dict | None:
    if not target:
        return None
    target_lower = target.lower()
    for b in beliefs:
        if target_lower in b["belief"].lower() or b["belief"].lower() in target_lower:
            return b
    target_words = set(target_lower.split())
    best, best_score = None, 0
    for b in beliefs:
        words = set(b["belief"].lower().split())
        overlap = len(target_words & words) / max(len(target_words | words), 1)
        if overlap > best_score and overlap > 0.4:
            best, best_score = b, overlap
    return best


def _similar(a: str, b: str) -> bool:
    wa = set(a.lower().split())
    wb = set(b.lower().split())
    overlap = len(wa & wb) / max(len(wa | wb), 1)
    return overlap > 0.6


def apply_delta(store: dict, delta: dict) -> dict:
    """Apply one conviction delta. Same logic as metabolism.py."""
    action = delta.get("action", "").lower()
    beliefs = store.get("convictions", [])
    now = datetime.now(timezone.utc).isoformat()

    if action == "add":
        belief_text = delta.get("belief", "").strip()
        if not belief_text:
            return store
        # Check for duplicates — strengthen instead
        for b in beliefs:
            if _similar(b["belief"], belief_text):
                amt = _strengthen_amount(b.get("confidence", 0.5))
                b["confidence"] = min(1.0, b.get("confidence", 0.5) + amt)
                b["times_reinforced"] = b.get("times_reinforced", 0) + 1
                b["last_reinforced"] = now
                store["last_updated"] = now
                store["deltas_applied"] = store.get("deltas_applied", 0) + 1
                return store

        initial_confidence = delta.get("initial_confidence", 0.5)
        beliefs.append({
            "belief": belief_text,
            "confidence": initial_confidence,
            "formed": now,
            "last_reinforced": now,
            "times_reinforced": 0,
            "source_campaign": delta.get("source_campaign", ""),
            "from_failure": delta.get("from_failure", False),
        })

        # Evict weakest if over cap
        if len(beliefs) > MAX_CONVICTIONS:
            evictable = [b for b in beliefs if b.get("confidence", 0.5) < SETTLED_THRESHOLD]
            if evictable:
                weakest = min(evictable, key=lambda b: b.get("confidence", 0))
                beliefs.remove(weakest)

    elif action == "strengthen":
        target = delta.get("target", "").strip()
        match = _find_conviction(beliefs, target)
        if match:
            amt = _strengthen_amount(match.get("confidence", 0.5))
            match["confidence"] = min(1.0, match.get("confidence", 0.5) + amt)
            match["times_reinforced"] = match.get("times_reinforced", 0) + 1
            match["last_reinforced"] = now

    elif action == "weaken":
        target = delta.get("target", "").strip()
        match = _find_conviction(beliefs, target)
        if match:
            # Settled convictions are protected
            if match.get("confidence", 0.5) >= SETTLED_THRESHOLD and match.get("times_reinforced", 0) >= 3:
                hit = _weaken_amount(match.get("confidence", 0.5)) * 0.5  # half damage
            else:
                hit = _weaken_amount(match.get("confidence", 0.5))
            match["confidence"] = max(0.0, match.get("confidence", 0.5) - hit)
            match["last_reinforced"] = now
            if match["confidence"] < 0.1:
                beliefs.remove(match)

    elif action == "revise":
        target = delta.get("target", "").strip()
        new_text = delta.get("belief", "").strip()
        match = _find_conviction(beliefs, target)
        if match and new_text:
            match["belief"] = new_text
            match["last_reinforced"] = now
            match["confidence"] = max(0.3, match.get("confidence", 0.5) - 0.05)

    elif action == "remove":
        target = delta.get("target", "").strip()
        match = _find_conviction(beliefs, target)
        if match:
            beliefs.remove(match)

    store["convictions"] = beliefs
    store["last_updated"] = now
    store["deltas_applied"] = store.get("deltas_applied", 0) + 1
    return store


# --- Extraction prompt ---

EXTRACT_PROMPT = """You are Stork's conviction engine. A campaign just finished.

Current convictions about how to work:
{convictions_text}

Campaign details:
  Goal: {goal}
  Outcome: {outcome}
  Duration: {duration}
  Cost: ${cost:.4f}
  Traces summary:
{traces_summary}

{post_mortem}

What is the SINGLE most important operational lesson from this campaign?

Rules:
1. Extract OPERATIONAL PATTERNS, not facts. "Chain runs degrade after step 4" is a pattern. "The campaign researched lacrosse stats" is a fact. Only patterns.
2. FAILURE LESSONS are more valuable. If this campaign failed, what should Stork do differently next time?
3. Before choosing ADD, check if any existing conviction covers this. If so, STRENGTHEN instead.
4. If this contradicts an existing conviction, WEAKEN or REVISE it.

Actions:
- ADD: new operational insight
- STRENGTHEN: existing conviction confirmed
- WEAKEN: existing conviction contradicted
- REVISE: existing conviction needs updating
- REMOVE: existing conviction proven wrong

If nothing operationally new was learned, respond with:
{{"action": "none"}}

Otherwise respond with exactly this JSON (no other text):
{{"action": "add|strengthen|weaken|revise|remove", "target": "existing conviction text (for strengthen/weaken/revise/remove, null for add)", "belief": "the conviction text (for add/revise)", "reason": "one sentence why"}}"""


def extract_conviction(goal: str, outcome: str, traces_summary: str,
                       cost: float = 0.0, duration: str = "",
                       post_mortem: str = "", campaign_id: str = "",
                       failed: bool = False) -> dict | None:
    """
    Call Haiku to extract one conviction delta from a completed campaign.
    Returns the delta dict, or None if no new conviction.
    """
    store = load_convictions()
    convictions_text = format_convictions_for_context(store)
    if not convictions_text:
        convictions_text = "(No convictions yet — fresh brain)"

    pm_section = ""
    if post_mortem:
        pm_section = f"Post-mortem (CAMPAIGN FAILED):\n{post_mortem}"

    prompt = EXTRACT_PROMPT.format(
        convictions_text=convictions_text,
        goal=goal[:500],
        outcome=outcome[:1000],
        duration=duration,
        cost=cost,
        traces_summary=traces_summary[:MAX_SUMMARY_CHARS],
        post_mortem=pm_section,
    )

    try:
        import anthropic
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=CONVICTION_MODEL,
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()

        # Strip markdown fences if present
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        delta = json.loads(raw)
        if delta.get("action") == "none":
            return None

        # Tag failure convictions with higher initial confidence
        if delta.get("action") == "add" and failed:
            delta["initial_confidence"] = 0.65  # failures start stronger
            delta["from_failure"] = True
        delta["source_campaign"] = campaign_id

        return delta

    except Exception as e:
        _log(f"Conviction extraction failed: {e}")
        return None


def apply_and_save(delta: dict) -> dict:
    """Apply a delta and persist. Returns updated store."""
    store = load_convictions()
    store = apply_delta(store, delta)
    save_convictions(store)
    return store


def digest_campaign(goal: str, outcome: str, traces_summary: str,
                    cost: float = 0.0, duration: str = "",
                    post_mortem: str = "", campaign_id: str = "",
                    failed: bool = False) -> dict | None:
    """Full pipeline: extract conviction → apply → save. Returns the delta or None."""
    delta = extract_conviction(
        goal=goal, outcome=outcome, traces_summary=traces_summary,
        cost=cost, duration=duration, post_mortem=post_mortem,
        campaign_id=campaign_id, failed=failed,
    )
    if delta is None:
        _log("No conviction extracted — nothing new learned.")
        return None

    action = delta.get("action", "?")
    belief = delta.get("belief", delta.get("target", ""))
    reason = delta.get("reason", "")
    _log(f"Conviction delta: {action} | {belief} | {reason}")

    apply_and_save(delta)
    n = len(load_convictions().get("convictions", []))
    _log(f"Convictions updated. {n} total beliefs.")
    return delta


def _log(msg: str):
    log_path = STORK_HOME / "conviction.log"
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass
