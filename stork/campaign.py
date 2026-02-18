"""
Stork Campaign — The executive function.
=========================================
The unit of work is a campaign, not a task.
A campaign produces exactly one artifact and learns one conviction.
Campaigns persist to disk, survive restarts, pick up where they left off.

Hugo & Watty · February 2026
"""

import asyncio
import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from stork.trace import AgentTrace, run_claude, run_many, run_chain
from stork.conviction import (
    digest_campaign, format_convictions_for_context,
    get_relevant_convictions, load_convictions,
)

STORK_HOME = Path(os.environ.get("STORK_HOME", os.path.expanduser("~/.stork")))
CAMPAIGNS_DIR = Path(os.environ.get("STORK_CAMPAIGNS_DIR", str(STORK_HOME / "campaigns")))
PLANNING_MODEL = os.environ.get("STORK_PLANNING_MODEL", "claude-haiku-4-5-20251001")

DEFAULT_BUDGET_PER_NIGHT = 2.00  # dollars


class Lane(str, Enum):
    NOW = "now"
    TONIGHT = "tonight"
    CAMPAIGN = "campaign"
    WATCH = "watch"


class Status(str, Enum):
    QUEUED = "queued"
    ACTIVE = "active"
    PAUSED = "paused"
    BLOCKED = "blocked"
    COMPLETE = "complete"
    FAILED = "failed"


@dataclass
class CampaignStep:
    description: str
    status: str = "pending"  # pending | active | done | failed | skipped
    trace_id: str = ""
    notes: str = ""


@dataclass
class Campaign:
    id: str
    name: str
    goal: str
    lane: str = Lane.NOW
    status: str = Status.QUEUED
    plan: list[dict] = field(default_factory=list)
    traces: list[dict] = field(default_factory=list)
    artifact_path: str = ""
    conviction: str = ""
    parent_campaign_id: str = ""
    budget_spent: float = 0.0
    budget_limit: float = DEFAULT_BUDGET_PER_NIGHT
    nights_active: int = 0
    created: str = ""
    updated: str = ""
    redirect_history: list[str] = field(default_factory=list)
    post_mortem: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "goal": self.goal,
            "lane": self.lane,
            "status": self.status,
            "plan": self.plan,
            "traces": self.traces,
            "artifact_path": self.artifact_path,
            "conviction": self.conviction,
            "parent_campaign_id": self.parent_campaign_id,
            "budget_spent": self.budget_spent,
            "budget_limit": self.budget_limit,
            "nights_active": self.nights_active,
            "created": self.created,
            "updated": self.updated,
            "redirect_history": self.redirect_history,
            "post_mortem": self.post_mortem,
        }

    @staticmethod
    def from_dict(d: dict) -> "Campaign":
        return Campaign(
            id=d.get("id", ""),
            name=d.get("name", ""),
            goal=d.get("goal", ""),
            lane=d.get("lane", Lane.NOW),
            status=d.get("status", Status.QUEUED),
            plan=d.get("plan", []),
            traces=d.get("traces", []),
            artifact_path=d.get("artifact_path", ""),
            conviction=d.get("conviction", ""),
            parent_campaign_id=d.get("parent_campaign_id", ""),
            budget_spent=d.get("budget_spent", 0.0),
            budget_limit=d.get("budget_limit", DEFAULT_BUDGET_PER_NIGHT),
            nights_active=d.get("nights_active", 0),
            created=d.get("created", ""),
            updated=d.get("updated", ""),
            redirect_history=d.get("redirect_history", []),
            post_mortem=d.get("post_mortem", ""),
        )

    def over_budget(self) -> bool:
        return self.budget_spent >= self.budget_limit

    def next_step(self) -> dict | None:
        for step in self.plan:
            if step.get("status") == "pending":
                return step
        return None

    def traces_summary(self, max_traces: int = 20) -> str:
        """Summarize traces for conviction extraction."""
        recent = self.traces[-max_traces:]
        lines = []
        for t in recent:
            status = t.get("status", "?")
            task = t.get("task", "")[:80]
            resp = t.get("response", "")[:150]
            errors = t.get("errors", [])
            if errors:
                lines.append(f"  [{status}] {task} -> ERROR: {'; '.join(errors[:2])}")
            else:
                lines.append(f"  [{status}] {task} -> {resp}")
        return "\n".join(lines) if lines else "(no traces)"


class CampaignManager:
    """Manages the full lifecycle of campaigns."""

    def __init__(self, campaigns_dir: Path | None = None):
        self.campaigns_dir = campaigns_dir or CAMPAIGNS_DIR
        self.campaigns_dir.mkdir(parents=True, exist_ok=True)
        self._campaigns: dict[str, Campaign] = {}
        self._load_all()

    def _load_all(self):
        """Load all campaigns from disk."""
        for f in self.campaigns_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                c = Campaign.from_dict(data)
                self._campaigns[c.id] = c
            except (json.JSONDecodeError, OSError, KeyError):
                continue

    def _save(self, campaign: Campaign):
        """Persist one campaign to disk."""
        campaign.updated = datetime.now(timezone.utc).isoformat()
        path = self.campaigns_dir / f"{campaign.id}.json"
        path.write_text(json.dumps(campaign.to_dict(), indent=2), encoding="utf-8")

    def get(self, campaign_id: str) -> Campaign | None:
        return self._campaigns.get(campaign_id)

    def list_all(self, lane: str | None = None, status: str | None = None) -> list[Campaign]:
        result = list(self._campaigns.values())
        if lane:
            result = [c for c in result if c.lane == lane]
        if status:
            result = [c for c in result if c.status == status]
        return sorted(result, key=lambda c: c.created or "", reverse=True)

    def create_campaign(self, goal: str, lane: str = Lane.NOW,
                        name: str = "", budget_limit: float | None = None,
                        parent_id: str = "") -> Campaign:
        """Create and queue a new campaign."""
        cid = str(uuid.uuid4())[:8]
        now = datetime.now(timezone.utc).isoformat()
        campaign = Campaign(
            id=cid,
            name=name or goal[:60],
            goal=goal,
            lane=lane,
            status=Status.QUEUED,
            budget_limit=budget_limit if budget_limit is not None else DEFAULT_BUDGET_PER_NIGHT,
            parent_campaign_id=parent_id,
            created=now,
            updated=now,
        )
        self._campaigns[cid] = campaign
        self._save(campaign)
        return campaign

    async def plan_campaign(self, campaign: Campaign) -> Campaign:
        """Use an LLM call to decompose the campaign goal into steps."""
        convictions = format_convictions_for_context()
        relevant = get_relevant_convictions(campaign.goal)
        relevant_text = ""
        if relevant:
            relevant_text = "\n\nRelevant lessons from past campaigns:\n"
            relevant_text += "\n".join(f"  - {b['belief']}" for b in relevant)

        prompt = f"""You are Stork, a campaign planner. Break this goal into concrete steps.
Each step should be a single agent task that produces a clear intermediate result.
Keep it to 3-7 steps. Be specific about what each step does.

{convictions}
{relevant_text}

Goal: {campaign.goal}

Respond with ONLY a JSON array of objects, each with "description" (string). No other text.
Example: [{{"description": "Search for X and compile findings"}}, {{"description": "Synthesize into report"}}]"""

        trace = await run_claude(prompt, topology="planner")
        campaign.traces.append(trace.to_dict())
        campaign.budget_spent += trace.cost_usd

        # Parse the plan from response
        try:
            raw = trace.response
            start = raw.index("[")
            end = raw.rindex("]") + 1
            steps = json.loads(raw[start:end])
            if isinstance(steps, list) and steps:
                campaign.plan = [
                    {"description": s.get("description", str(s)), "status": "pending"}
                    for s in steps
                ]
            else:
                raise ValueError("Empty plan")
        except (ValueError, json.JSONDecodeError):
            # Fallback: single step = just do the thing
            campaign.plan = [{"description": campaign.goal, "status": "pending"}]

        self._save(campaign)
        return campaign

    async def execute_step(self, campaign: Campaign, step: dict) -> AgentTrace:
        """Execute one step of a campaign."""
        if campaign.over_budget():
            campaign.status = Status.BLOCKED
            step["status"] = "skipped"
            step["notes"] = "Budget exceeded"
            self._save(campaign)
            return AgentTrace(
                id="budget-blocked",
                task=step["description"],
                status="error",
                errors=[f"Budget limit ${campaign.budget_limit:.2f} exceeded (spent ${campaign.budget_spent:.2f})"],
            )

        step["status"] = "active"
        self._save(campaign)

        # Build context from previous completed steps
        context_parts = []
        for s in campaign.plan:
            if s.get("status") == "done" and s.get("notes"):
                context_parts.append(f"Completed: {s['description']}\nResult: {s['notes'][:500]}")

        task = step["description"]
        if context_parts:
            task = f"CAMPAIGN: {campaign.goal}\n\nPrevious work:\n" + "\n---\n".join(context_parts) + f"\n\nYOUR TASK: {task}"
        else:
            task = f"CAMPAIGN: {campaign.goal}\n\nYOUR TASK: {task}"

        step_idx = campaign.plan.index(step) + 1
        total = len(campaign.plan)
        trace = await run_claude(task, topology=f"campaign:{step_idx}/{total}")

        # Record
        step["trace_id"] = trace.id
        campaign.traces.append(trace.to_dict())
        campaign.budget_spent += trace.cost_usd

        if trace.succeeded():
            step["status"] = "done"
            step["notes"] = trace.response[:2000]
        else:
            step["status"] = "failed"
            step["notes"] = "; ".join(trace.errors) if trace.errors else "Unknown failure"

        self._save(campaign)
        return trace

    async def run_next(self) -> tuple[Campaign, AgentTrace] | None:
        """Pick highest priority campaign and execute its next step."""
        # Priority: NOW > active TONIGHT > active CAMPAIGN > queued TONIGHT > queued CAMPAIGN
        priority_order = [
            (Lane.NOW, Status.QUEUED),
            (Lane.NOW, Status.ACTIVE),
            (Lane.TONIGHT, Status.ACTIVE),
            (Lane.CAMPAIGN, Status.ACTIVE),
            (Lane.TONIGHT, Status.QUEUED),
            (Lane.CAMPAIGN, Status.QUEUED),
        ]

        target = None
        for lane, status in priority_order:
            candidates = self.list_all(lane=lane, status=status)
            if candidates:
                target = candidates[0]
                break

        if target is None:
            return None

        # If queued, plan it first
        if target.status == Status.QUEUED:
            target.status = Status.ACTIVE
            target = await self.plan_campaign(target)

        step = target.next_step()
        if step is None:
            # All steps done — campaign complete
            await self._complete(target)
            return target, AgentTrace(
                id="complete",
                task=f"Campaign {target.id} completed",
                status="done",
                response=f"All steps finished. Artifact: {target.artifact_path}",
            )

        trace = await self.execute_step(target, step)

        # Check if campaign is now complete
        if target.next_step() is None:
            await self._complete(target)

        return target, trace

    async def _complete(self, campaign: Campaign):
        """Finalize a campaign: set status, extract conviction, prune traces."""
        all_failed = all(s.get("status") == "failed" for s in campaign.plan)
        any_failed = any(s.get("status") == "failed" for s in campaign.plan)

        if all_failed:
            campaign.status = Status.FAILED
            campaign.post_mortem = self._write_post_mortem(campaign)
        else:
            campaign.status = Status.COMPLETE

        # Extract conviction
        outcome = "FAILED" if campaign.status == Status.FAILED else "SUCCESS"
        if any_failed and campaign.status != Status.FAILED:
            outcome = "PARTIAL SUCCESS (some steps failed)"

        delta = digest_campaign(
            goal=campaign.goal,
            outcome=outcome,
            traces_summary=campaign.traces_summary(),
            cost=campaign.budget_spent,
            duration=f"{campaign.nights_active} nights",
            post_mortem=campaign.post_mortem,
            campaign_id=campaign.id,
            failed=(campaign.status == Status.FAILED),
        )

        if delta:
            campaign.conviction = delta.get("belief", delta.get("target", ""))

        # Prune traces — the food is destroyed. Keep only summaries.
        pruned = []
        for t in campaign.traces:
            pruned.append({
                "id": t.get("id"),
                "task": t.get("task", "")[:200],
                "status": t.get("status"),
                "cost_usd": t.get("cost_usd", 0),
                "errors": t.get("errors", [])[:2],
            })
        campaign.traces = pruned

        self._save(campaign)

        # Unblock parent campaign if this was a sub-campaign
        if campaign.parent_campaign_id:
            self.unblock_parent(campaign.id)

    def _write_post_mortem(self, campaign: Campaign) -> str:
        """Generate a post-mortem for a failed campaign."""
        lines = [f"POST-MORTEM: {campaign.name}"]
        lines.append(f"Goal: {campaign.goal}")
        lines.append(f"Budget spent: ${campaign.budget_spent:.4f} / ${campaign.budget_limit:.2f}")
        lines.append(f"Nights active: {campaign.nights_active}")
        lines.append("")
        lines.append("Steps attempted:")
        for i, step in enumerate(campaign.plan, 1):
            status = step.get("status", "?")
            desc = step.get("description", "")
            notes = step.get("notes", "")
            lines.append(f"  {i}. [{status}] {desc}")
            if notes and status == "failed":
                lines.append(f"     Error: {notes[:200]}")
        lines.append("")
        lines.append("What to do differently next time: (extracted as conviction)")
        return "\n".join(lines)

    def pause(self, campaign_id: str) -> Campaign | None:
        """Pause a running campaign."""
        c = self.get(campaign_id)
        if c and c.status in (Status.ACTIVE, Status.QUEUED):
            c.status = Status.PAUSED
            self._save(c)
        return c

    def redirect(self, campaign_id: str, new_instruction: str) -> Campaign | None:
        """Hugo sends one sentence — campaign pivots."""
        c = self.get(campaign_id)
        if c is None:
            return None

        c.redirect_history.append(f"{datetime.now(timezone.utc).isoformat()}: {new_instruction}")

        # Mark remaining pending steps as skipped
        for step in c.plan:
            if step.get("status") == "pending":
                step["status"] = "skipped"
                step["notes"] = f"Redirected: {new_instruction}"

        # Add new step based on redirect
        c.plan.append({
            "description": f"REDIRECT — {new_instruction} (original goal: {c.goal})",
            "status": "pending",
        })

        if c.status == Status.PAUSED:
            c.status = Status.ACTIVE
        self._save(c)
        return c

    def kill(self, campaign_id: str) -> Campaign | None:
        """Stop a campaign and write its post-mortem."""
        c = self.get(campaign_id)
        if c is None:
            return None

        c.status = Status.FAILED
        c.post_mortem = self._write_post_mortem(c)

        # Still extract a conviction from failure
        delta = digest_campaign(
            goal=c.goal,
            outcome="KILLED by Hugo",
            traces_summary=c.traces_summary(),
            cost=c.budget_spent,
            post_mortem=c.post_mortem,
            campaign_id=c.id,
            failed=True,
        )
        if delta:
            c.conviction = delta.get("belief", delta.get("target", ""))

        self._save(c)
        return c

    def spawn_sub(self, parent_id: str, sub_goal: str) -> Campaign | None:
        """Campaign discovered a prerequisite — spawn a sub-campaign."""
        parent = self.get(parent_id)
        if parent is None:
            return None

        # Pause parent while sub runs
        parent.status = Status.BLOCKED
        self._save(parent)

        sub = self.create_campaign(
            goal=sub_goal,
            lane=parent.lane,
            name=f"Sub: {sub_goal[:50]}",
            budget_limit=parent.budget_limit * 0.3,  # Sub gets 30% of parent budget
            parent_id=parent_id,
        )
        return sub

    def negotiate(self) -> list[str]:
        """
        Evaluate active campaigns. Judgment, not scheduling.
        Returns list of actions taken.
        """
        actions = []
        active = self.list_all(status=Status.ACTIVE)

        if len(active) <= 1:
            return actions

        # Sort by value: closer to finishing = higher priority
        for c in active:
            total = len(c.plan) or 1
            done = sum(1 for s in c.plan if s.get("status") == "done")
            c._completion = done / total
            c._stuck = all(
                s.get("status") == "failed"
                for s in c.plan[-3:]
                if s.get("status") in ("done", "failed")
            ) if len(c.plan) >= 3 else False

        # Kill stuck campaigns
        for c in active:
            if getattr(c, "_stuck", False):
                self.kill(c.id)
                actions.append(f"Killed stuck campaign {c.id}: {c.name}")

        # Pause expensive ones if budget pressure
        active = [c for c in active if c.status == Status.ACTIVE]
        if len(active) > 2:
            by_cost = sorted(active, key=lambda c: c.budget_spent, reverse=True)
            for expensive in by_cost[2:]:  # Keep top 2, pause rest
                if getattr(expensive, "_completion", 0) < 0.5:
                    self.pause(expensive.id)
                    actions.append(f"Paused expensive campaign {expensive.id}: {expensive.name}")

        return actions

    async def run_overnight(self, lanes: list[str] | None = None) -> list[tuple[str, str]]:
        """
        Process TONIGHT and CAMPAIGN lanes until empty or budget exhausted.
        Returns list of (campaign_id, outcome) tuples.
        """
        lanes = lanes or [Lane.TONIGHT, Lane.CAMPAIGN]
        results = []

        # Negotiate before starting
        self.negotiate()

        max_iterations = 100  # safety valve
        for _ in range(max_iterations):
            candidates = []
            for lane in lanes:
                candidates.extend(self.list_all(lane=lane, status=Status.QUEUED))
                candidates.extend(self.list_all(lane=lane, status=Status.ACTIVE))

            if not candidates:
                break

            result = await self.run_next()
            if result is None:
                break

            campaign, trace = result
            if campaign.status in (Status.COMPLETE, Status.FAILED):
                results.append((campaign.id, campaign.status))

            # Re-negotiate periodically
            if len(results) % 5 == 0:
                self.negotiate()

        return results

    def unblock_parent(self, campaign_id: str):
        """When a sub-campaign completes, unblock its parent."""
        c = self.get(campaign_id)
        if c and c.parent_campaign_id:
            parent = self.get(c.parent_campaign_id)
            if parent and parent.status == Status.BLOCKED:
                parent.status = Status.ACTIVE
                self._save(parent)
