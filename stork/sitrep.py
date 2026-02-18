"""
Stork Sit-Rep — The morning report.
=====================================
Three sections, three paragraphs max total.
Ends with exactly ONE question. Never more.
Forces Hugo to engage for 30 seconds, gives Stork a steering signal.

Hugo & Watty · February 2026
"""

import json
import os
from datetime import datetime, timezone, date
from pathlib import Path

STORK_HOME = Path(os.environ.get("STORK_HOME", os.path.expanduser("~/.stork")))
SITREPS_DIR = Path(os.environ.get("STORK_SITREPS_DIR", "stork/sitreps"))
SITREP_MODEL = os.environ.get("STORK_SITREP_MODEL", "claude-haiku-4-5-20251001")

SITREP_PROMPT = """You are Stork writing a morning sit-rep for Hugo. Be direct, specific, concise.

Overnight results:
{results}

Current convictions:
{convictions}

Active/queued campaigns:
{campaigns}

Write a sit-rep with EXACTLY three sections:

DONE: What was accomplished overnight. Reference specific artifacts/files if any exist.
BLOCKED: What needs Hugo's input. Be specific about what decision is needed. If nothing is blocked, say "Nothing blocked."
NEXT: What Stork will do tonight if Hugo doesn't redirect.

Rules:
- Three paragraphs MAX total across all sections.
- End with EXACTLY ONE question that helps Hugo steer. Make it a real choice, not "any feedback?"
- No fluff, no greetings, no sign-offs.
- Reference campaign names and artifacts specifically.

Format:
## DONE
(paragraph)

## BLOCKED
(paragraph)

## NEXT
(paragraph)

(one question)"""


def generate_sitrep(results: list[dict], campaigns: list[dict],
                    convictions_text: str = "") -> str:
    """
    Generate a sit-rep from overnight results. Uses Haiku to write it.
    Falls back to a structured template if the API call fails.
    """
    results_text = _format_results(results)
    campaigns_text = _format_campaigns(campaigns)

    prompt = SITREP_PROMPT.format(
        results=results_text,
        convictions=convictions_text or "(no convictions yet)",
        campaigns=campaigns_text,
    )

    try:
        import anthropic
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=SITREP_MODEL,
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()
    except Exception as e:
        # Fallback: structured template without LLM
        return _fallback_sitrep(results, campaigns)


def _format_results(results: list[dict]) -> str:
    if not results:
        return "(no campaigns ran overnight)"
    lines = []
    for r in results:
        cid = r.get("id", "?")
        name = r.get("name", "")
        status = r.get("status", "?")
        cost = r.get("budget_spent", 0)
        artifact = r.get("artifact_path", "")
        conviction = r.get("conviction", "")
        line = f"  [{status}] {name} (${cost:.4f})"
        if artifact:
            line += f" -> {artifact}"
        if conviction:
            line += f" | Learned: {conviction}"
        lines.append(line)
    return "\n".join(lines)


def _format_campaigns(campaigns: list[dict]) -> str:
    if not campaigns:
        return "(no active campaigns)"
    lines = []
    for c in campaigns:
        status = c.get("status", "?")
        name = c.get("name", "")
        lane = c.get("lane", "")
        total = len(c.get("plan", []))
        done = sum(1 for s in c.get("plan", []) if s.get("status") == "done")
        lines.append(f"  [{status}] {name} ({lane}) — {done}/{total} steps")
    return "\n".join(lines)


def _fallback_sitrep(results: list[dict], campaigns: list[dict]) -> str:
    """Structured fallback when Haiku isn't available."""
    lines = []

    lines.append("## DONE")
    completed = [r for r in results if r.get("status") == "complete"]
    failed = [r for r in results if r.get("status") == "failed"]
    if completed:
        names = ", ".join(r.get("name", "?") for r in completed)
        lines.append(f"Completed {len(completed)} campaign(s): {names}.")
    elif failed:
        lines.append(f"{len(failed)} campaign(s) failed overnight. Post-mortems written.")
    else:
        lines.append("No campaigns ran overnight.")

    lines.append("")
    lines.append("## BLOCKED")
    blocked = [c for c in campaigns if c.get("status") == "blocked"]
    if blocked:
        for b in blocked:
            lines.append(f"{b.get('name', '?')} needs input: check its plan for details.")
    else:
        lines.append("Nothing blocked.")

    lines.append("")
    lines.append("## NEXT")
    queued = [c for c in campaigns if c.get("status") in ("queued", "active")]
    if queued:
        names = ", ".join(c.get("name", "?") for c in queued[:3])
        lines.append(f"Tonight: continue {names}.")
    else:
        lines.append("Campaign queue is empty. Waiting for direction.")

    lines.append("")
    lines.append("Should I prioritize finishing existing work or start something new tonight?")
    return "\n".join(lines)


def save_sitrep(content: str, sitrep_date: date | None = None) -> Path:
    """Save sit-rep to disk. Returns the path."""
    sitrep_date = sitrep_date or date.today()
    SITREPS_DIR.mkdir(parents=True, exist_ok=True)
    path = SITREPS_DIR / f"{sitrep_date.isoformat()}.md"
    path.write_text(content, encoding="utf-8")
    return path


def load_sitrep(sitrep_date: date | None = None) -> str | None:
    """Load a sit-rep by date. Returns content or None."""
    sitrep_date = sitrep_date or date.today()
    path = SITREPS_DIR / f"{sitrep_date.isoformat()}.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return None


def create_sitrep(manager, convictions_text: str = "") -> str:
    """
    Full pipeline: gather data from CampaignManager, generate, save.
    Returns the sit-rep content.
    """
    # Gather completed/failed campaigns from recent overnight
    all_campaigns = manager.list_all()
    results = []
    for c in all_campaigns:
        if c.status in ("complete", "failed"):
            results.append(c.to_dict())

    active = []
    for c in all_campaigns:
        if c.status in ("queued", "active", "blocked", "paused"):
            active.append(c.to_dict())

    content = generate_sitrep(results, active, convictions_text)
    path = save_sitrep(content)
    return content
