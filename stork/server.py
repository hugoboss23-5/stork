"""
Stork Server — The MCP interface.
===================================
Replaces agent_army.py completely. Same base tools, upgraded with campaigns.
Binds 127.0.0.1 NOT 0.0.0.0.
On shutdown: kill all child processes properly.

Hugo & Watty · February 2026
"""

import asyncio
import json
import os
import signal
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP

import traceback

from stork.trace import run_claude, run_many, run_chain, AgentTrace
from stork.campaign import CampaignManager, Campaign, Lane, Status
from stork.conviction import format_convictions_for_context, load_convictions
from stork.sitrep import create_sitrep, load_sitrep, generate_sitrep, save_sitrep
from stork.boredom import check_and_propose, record_activity
from stork.profiles import list_profiles, load_profiles

PORT = int(os.environ.get("STORK_PORT", "8000"))
HOST = "127.0.0.1"

mcp = FastMCP(
    name="stork",
    host=HOST,
    port=PORT,
    streamable_http_path="/mcp",
    sse_path="/sse",
    stateless_http=True,
)

_manager: CampaignManager | None = None
_running_agents: dict[str, AgentTrace] = {}
_overnight_task: asyncio.Task | None = None
_MAX_AGENT_TRACES = 100


def _get_manager() -> CampaignManager:
    global _manager
    if _manager is None:
        _manager = CampaignManager()
    return _manager


def _track_agent(trace: AgentTrace):
    """Register a trace and prune oldest entries if over cap."""
    _running_agents[trace.id] = trace
    if len(_running_agents) > _MAX_AGENT_TRACES:
        # Drop oldest entries (dict preserves insertion order)
        excess = len(_running_agents) - _MAX_AGENT_TRACES
        for key in list(_running_agents)[:excess]:
            del _running_agents[key]


# ──────────────────────────────────────────────
# NOW lane tools — direct agent operations
# ──────────────────────────────────────────────

@mcp.tool()
async def spawn(task: str, timeout_seconds: int = 0, profile: str = "") -> str:
    """NOW lane. Single agent. Optional timeout_seconds and profile (e.g. 'quant', 'research', 'code', 'fast')."""
    try:
        timeout = timeout_seconds if timeout_seconds > 0 else None
        trace = await run_claude(task, topology="solo", timeout=timeout, profile=profile)
        _track_agent(trace)
        record_activity()

        if trace.succeeded():
            return json.dumps({
                "status": "done",
                "agent_id": trace.id,
                "response": trace.response,
                "thinking": trace.thinking[:2000] if trace.thinking else "",
                "cost_usd": trace.cost_usd,
                "duration_ms": trace.duration_ms,
                "tokens": {"in": trace.tokens_in, "out": trace.tokens_out},
            }, indent=2)
        else:
            return json.dumps({
                "status": trace.status,
                "agent_id": trace.id,
                "errors": trace.errors,
                "stderr": getattr(trace, "_stderr", ""),
                "cost_usd": trace.cost_usd,
                "duration_ms": trace.duration_ms,
            }, indent=2)
    except Exception as e:
        _log(f"spawn error: {e}")
        return json.dumps({
            "status": "error",
            "error": f"{type(e).__name__}: {e}",
            "traceback": traceback.format_exc()[-500:],
        }, indent=2)


@mcp.tool()
async def spawn_many(tasks: list[str], timeout_seconds: int = 0, profile: str = "") -> str:
    """NOW lane. Parallel agents. Returns per-agent status so you can see WHICH failed and WHY."""
    try:
        if len(tasks) > 10:
            return json.dumps({"error": "Max 10 parallel tasks."})

        timeout = timeout_seconds if timeout_seconds > 0 else None
        traces = await run_many(tasks, timeout=timeout, profile=profile)
        for t in traces:
            _track_agent(t)
        record_activity()

        results = []
        for task_str, trace in zip(tasks, traces):
            entry = {
                "task": task_str[:200],
                "agent_id": trace.id,
                "status": trace.status,
                "response": trace.response[:3000] if trace.response else "",
                "cost_usd": trace.cost_usd,
                "duration_ms": trace.duration_ms,
            }
            if trace.errors:
                entry["errors"] = trace.errors
            if hasattr(trace, "_stderr") and trace._stderr:
                entry["stderr"] = trace._stderr[:500]
            results.append(entry)

        succeeded = sum(1 for t in traces if t.succeeded())
        total_cost = sum(t.cost_usd for t in traces)
        return json.dumps({
            "agents": len(traces),
            "succeeded": succeeded,
            "failed": len(traces) - succeeded,
            "total_cost_usd": total_cost,
            "results": results,
        }, indent=2)
    except Exception as e:
        _log(f"spawn_many error: {e}")
        return json.dumps({
            "status": "error",
            "error": f"{type(e).__name__}: {e}",
            "traceback": traceback.format_exc()[-500:],
        }, indent=2)


@mcp.tool()
async def chain(steps: list[str], timeout_seconds: int = 0, profile: str = "") -> str:
    """NOW lane. Sequential pipeline. Shows all steps including ones that didn't run if chain broke."""
    try:
        if len(steps) > 7:
            return json.dumps({"error": "Max 7 chain steps."})

        timeout = timeout_seconds if timeout_seconds > 0 else None
        traces = await run_chain(steps, timeout=timeout, profile=profile)
        for t in traces:
            _track_agent(t)
        record_activity()

        results = []
        for i, step_str in enumerate(steps):
            if i < len(traces):
                trace = traces[i]
                entry = {
                    "step": step_str[:200],
                    "agent_id": trace.id,
                    "status": trace.status,
                    "response": trace.response[:3000] if trace.response else "",
                }
                if trace.errors:
                    entry["errors"] = trace.errors
                if hasattr(trace, "_stderr") and trace._stderr:
                    entry["stderr"] = trace._stderr[:500]
            else:
                entry = {
                    "step": step_str[:200],
                    "agent_id": None,
                    "status": "not_run",
                    "response": "",
                    "errors": ["Chain broke before this step"],
                }
            results.append(entry)

        total_cost = sum(t.cost_usd for t in traces)
        chain_broke = len(traces) < len(steps)
        return json.dumps({
            "steps_completed": sum(1 for t in traces if t.succeeded()),
            "steps_attempted": len(traces),
            "steps_total": len(steps),
            "chain_broke": chain_broke,
            "total_cost_usd": total_cost,
            "results": results,
        }, indent=2)
    except Exception as e:
        _log(f"chain error: {e}")
        return json.dumps({
            "status": "error",
            "error": f"{type(e).__name__}: {e}",
            "traceback": traceback.format_exc()[-500:],
        }, indent=2)


@mcp.tool()
async def delegate(goal: str, num_agents: int = 3, timeout_seconds: int = 0, profile: str = "") -> str:
    """NOW lane. Plan → parallel execute → synthesize. Reports which phase failed and why."""
    try:
        num_agents = min(max(num_agents, 2), 5)
        timeout = timeout_seconds if timeout_seconds > 0 else None

        # ── Phase 1: Plan ──
        _log(f"delegate: planning with {num_agents} agents for: {goal[:80]}")
        plan_trace = await run_claude(
            f"Break this goal into exactly {num_agents} independent subtasks. "
            f"Return ONLY a JSON array of strings, no other text.\n\nGoal: {goal}",
            topology="delegate:planner",
            timeout=timeout,
            profile=profile,
        )

        if not plan_trace.succeeded():
            return json.dumps({
                "status": "error",
                "phase": "planning",
                "error": "; ".join(plan_trace.errors) or "Planner failed with no error detail",
                "agent_id": plan_trace.id,
                "cost_usd": plan_trace.cost_usd,
                "duration_ms": plan_trace.duration_ms,
            }, indent=2)

        try:
            raw = plan_trace.response
            start = raw.index("[")
            end = raw.rindex("]") + 1
            subtasks = json.loads(raw[start:end])
            if not isinstance(subtasks, list) or not subtasks:
                raise ValueError("Empty")
        except (ValueError, json.JSONDecodeError):
            subtasks = [f"Part {i+1} of '{goal}'" for i in range(num_agents)]

        # ── Phase 2: Execute ──
        _log(f"delegate: executing {len(subtasks)} subtasks")
        worker_traces = await run_many(subtasks, timeout=timeout, profile=profile)

        succeeded_workers = []
        failed_workers = []
        for subtask, trace in zip(subtasks, worker_traces):
            if trace.succeeded():
                succeeded_workers.append((subtask, trace))
            else:
                failed_workers.append((subtask, trace))

        if not succeeded_workers:
            # ALL workers failed
            return json.dumps({
                "status": "error",
                "phase": "execution",
                "error": "All worker agents failed",
                "failed_subtasks": [
                    {
                        "task": s[:200],
                        "agent_id": t.id,
                        "status": t.status,
                        "errors": t.errors,
                    }
                    for s, t in failed_workers
                ],
                "cost_usd": sum(t.cost_usd for t in worker_traces) + plan_trace.cost_usd,
            }, indent=2)

        # ── Phase 3: Synthesize ──
        _log(f"delegate: synthesizing ({len(succeeded_workers)} succeeded, {len(failed_workers)} failed)")
        synthesis_input = f"ORIGINAL GOAL: {goal}\n\n"
        for i, (subtask, trace) in enumerate(succeeded_workers):
            synthesis_input += f"--- SUBTASK: {subtask} ---\n{trace.response[:2000]}\n\n"
        if failed_workers:
            synthesis_input += f"NOTE: {len(failed_workers)} subtask(s) failed and are not included.\n\n"
        synthesis_input += "Synthesize all subtask results into a single coherent response."

        synth_trace = await run_claude(synthesis_input, topology="delegate:synthesizer",
                                       timeout=timeout, profile=profile)

        if not synth_trace.succeeded():
            return json.dumps({
                "status": "error",
                "phase": "synthesis",
                "error": "; ".join(synth_trace.errors) or "Synthesizer failed with no error detail",
                "agent_id": synth_trace.id,
                "partial_results": [t.response[:1000] for _, t in succeeded_workers],
                "cost_usd": sum(t.cost_usd for t in worker_traces) + plan_trace.cost_usd + synth_trace.cost_usd,
            }, indent=2)

        record_activity()

        all_traces = [plan_trace] + list(worker_traces) + [synth_trace]
        total_cost = sum(t.cost_usd for t in all_traces)

        result = {
            "status": "done",
            "goal": goal,
            "agents_used": len(subtasks) + 2,
            "succeeded_workers": len(succeeded_workers),
            "failed_workers": len(failed_workers),
            "total_cost_usd": total_cost,
            "subtasks": subtasks,
            "result": synth_trace.response[:5000],
        }

        if failed_workers:
            result["warnings"] = [
                f"Subtask failed: {s[:80]} ({t.status}: {'; '.join(t.errors[:2])})"
                for s, t in failed_workers
            ]

        return json.dumps(result, indent=2)
    except Exception as e:
        _log(f"delegate error: {e}")
        return json.dumps({
            "status": "error",
            "phase": "unknown",
            "error": f"{type(e).__name__}: {e}",
            "traceback": traceback.format_exc()[-500:],
        }, indent=2)


# ──────────────────────────────────────────────
# Profile tools
# ──────────────────────────────────────────────

@mcp.tool()
async def profiles() -> str:
    """List available agent profiles with their system prompts and timeouts."""
    all_profiles = load_profiles()
    result = {}
    for name, spec in sorted(all_profiles.items()):
        result[name] = {
            "system_prompt": (spec.get("system_prompt") or "(default)")[:200],
            "timeout": spec.get("timeout", 300),
        }
    return json.dumps(result, indent=2)


# ──────────────────────────────────────────────
# Campaign tools
# ──────────────────────────────────────────────

@mcp.tool()
async def campaign_create(goal: str, lane: str = "tonight") -> str:
    """Create a campaign. Lane: now, tonight, campaign, watch."""
    mgr = _get_manager()
    valid_lanes = {"now", "tonight", "campaign", "watch"}
    if lane.lower() not in valid_lanes:
        return json.dumps({"error": f"Invalid lane. Choose: {', '.join(valid_lanes)}"})

    # TODO: WATCH lane has no implementation yet (v0.1) — campaigns are created
    # but never polled or triggered. Needs a watcher loop that periodically
    # checks a condition and activates the campaign when met.
    c = mgr.create_campaign(goal=goal, lane=lane.lower())
    record_activity()

    # If NOW lane, plan and run all steps to completion
    if lane.lower() == "now":
        c = await mgr.plan_campaign(c)
        c.status = Status.ACTIVE
        while True:
            step = c.next_step()
            if step is None:
                break
            trace = await mgr.execute_step(c, step)
            if c.over_budget() or c.status in (Status.BLOCKED, Status.FAILED):
                break
        # Finalize if all steps are done
        if c.next_step() is None and c.status not in (Status.COMPLETE, Status.FAILED):
            await mgr._complete(c)

    return json.dumps({
        "campaign_id": c.id,
        "name": c.name,
        "lane": c.lane,
        "status": c.status,
        "steps": len(c.plan),
    }, indent=2)


@mcp.tool()
async def campaign_status(campaign_id: str = "") -> str:
    """Status of one or all campaigns."""
    mgr = _get_manager()

    if campaign_id:
        c = mgr.get(campaign_id)
        if c is None:
            return json.dumps({"error": f"Campaign {campaign_id} not found"})
        return json.dumps(c.to_dict(), indent=2)
    else:
        campaigns = mgr.list_all()
        return json.dumps([{
            "id": c.id,
            "name": c.name,
            "lane": c.lane,
            "status": c.status,
            "budget_spent": c.budget_spent,
            "steps_done": sum(1 for s in c.plan if s.get("status") == "done"),
            "steps_total": len(c.plan),
            "conviction": c.conviction,
        } for c in campaigns], indent=2)


@mcp.tool()
async def campaign_redirect(campaign_id: str, instruction: str) -> str:
    """One sentence pivot. Hugo says, campaign adjusts."""
    mgr = _get_manager()
    c = mgr.redirect(campaign_id, instruction)
    if c is None:
        return json.dumps({"error": f"Campaign {campaign_id} not found"})
    return json.dumps({
        "campaign_id": c.id,
        "status": c.status,
        "redirected": True,
        "new_step": c.plan[-1]["description"] if c.plan else "",
    }, indent=2)


@mcp.tool()
async def campaign_kill(campaign_id: str) -> str:
    """Stop a campaign. Writes post-mortem. Extracts failure conviction."""
    mgr = _get_manager()
    c = mgr.kill(campaign_id)
    if c is None:
        return json.dumps({"error": f"Campaign {campaign_id} not found"})
    return json.dumps({
        "campaign_id": c.id,
        "status": c.status,
        "conviction": c.conviction,
        "post_mortem": c.post_mortem[:500],
    }, indent=2)


# ──────────────────────────────────────────────
# Overnight operations
# ──────────────────────────────────────────────

@mcp.tool()
async def overnight_start() -> str:
    """Begin processing TONIGHT + CAMPAIGN lanes."""
    global _overnight_task
    mgr = _get_manager()

    if _overnight_task and not _overnight_task.done():
        return json.dumps({"error": "Overnight session already running"})

    async def _run_overnight():
        try:
            results = await mgr.run_overnight()
            convictions_text = format_convictions_for_context()
            content = create_sitrep(mgr, convictions_text)

            # Check for boredom-driven proposals
            proposal = check_and_propose(mgr, convictions_text)
            if proposal:
                content += f"\n\n---\n*Initiative:* {proposal}"
                # Re-save with proposal appended
                from stork.sitrep import save_sitrep
                save_sitrep(content)

            # Persist overnight results so sitrep/status can reference them
            _save_overnight_results(results)

            return results
        except Exception as e:
            _log(f"Overnight run error: {e}")
            _save_overnight_results([], error=str(e))
            return []

    _overnight_task = asyncio.create_task(_run_overnight())
    return json.dumps({"status": "started", "message": "Overnight session running"})


@mcp.tool()
async def overnight_stop() -> str:
    """Graceful shutdown. Save state, write sit-rep."""
    global _overnight_task
    mgr = _get_manager()

    if _overnight_task and not _overnight_task.done():
        _overnight_task.cancel()
        try:
            await _overnight_task
        except asyncio.CancelledError:
            pass

    # Pause any active campaigns
    for c in mgr.list_all(status="active"):
        mgr.pause(c.id)

    # Generate sit-rep for what we have
    convictions_text = format_convictions_for_context()
    content = create_sitrep(mgr, convictions_text)

    return json.dumps({
        "status": "stopped",
        "sitrep_generated": True,
        "active_campaigns_paused": len(mgr.list_all(status="paused")),
    }, indent=2)


# ──────────────────────────────────────────────
# Read-only tools
# ──────────────────────────────────────────────

@mcp.tool()
async def convictions() -> str:
    """Read current beliefs as natural language."""
    text = format_convictions_for_context()
    if not text:
        return "No convictions yet. Stork hasn't completed any campaigns."

    store = load_convictions()
    n = len(store.get("convictions", []))
    deltas = store.get("deltas_applied", 0)
    return f"{text}\n\n({n} convictions, {deltas} total updates)"


@mcp.tool()
async def sitrep(date_str: str = "") -> str:
    """Read a sit-rep. Defaults to today."""
    if date_str:
        try:
            d = date.fromisoformat(date_str)
        except ValueError:
            return json.dumps({"error": f"Invalid date: {date_str}. Use YYYY-MM-DD."})
    else:
        d = date.today()

    content = load_sitrep(d)
    if content is None:
        return f"No sit-rep for {d.isoformat()}."
    return content


@mcp.tool()
async def status() -> str:
    """All agents currently running + campaign overview + elapsed time for running agents."""
    mgr = _get_manager()
    now = datetime.now(timezone.utc)

    running = {k: v for k, v in _running_agents.items() if v.status == "running"}
    done = {k: v for k, v in _running_agents.items() if v.status == "done"}
    errored = {k: v for k, v in _running_agents.items() if v.status in ("error", "timeout")}

    # Show detail for running agents including elapsed time
    running_detail = []
    for tid, trace in running.items():
        elapsed_s = 0
        if trace.started_at:
            try:
                started = datetime.fromisoformat(trace.started_at)
                elapsed_s = int((now - started).total_seconds())
            except (ValueError, TypeError):
                pass
        running_detail.append({
            "agent_id": tid,
            "task": trace.task[:100],
            "topology": trace.topology_position,
            "elapsed_seconds": elapsed_s,
        })

    campaigns = mgr.list_all()
    active_campaigns = [c for c in campaigns if c.status in ("active", "queued")]

    overnight_running = _overnight_task is not None and not _overnight_task.done()

    result = {
        "agents": {
            "running": len(running),
            "running_detail": running_detail,
            "done": len(done),
            "errored": len(errored),
            "total": len(_running_agents),
        },
        "campaigns": {
            "total": len(campaigns),
            "active": len(active_campaigns),
            "by_status": _count_by(campaigns, "status"),
            "by_lane": _count_by(campaigns, "lane"),
        },
        "overnight_running": overnight_running,
    }

    last_overnight = _load_overnight_results()
    if last_overnight:
        result["last_overnight"] = last_overnight

    return json.dumps(result, indent=2)


def _count_by(items: list, attr: str) -> dict:
    counts = {}
    for item in items:
        val = getattr(item, attr, "?")
        counts[val] = counts.get(val, 0) + 1
    return counts


def _save_overnight_results(results: list, error: str = ""):
    """Persist overnight results to disk for sitrep/status access."""
    from stork.conviction import STORK_HOME
    path = STORK_HOME / "last_overnight.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "results": [
                {"campaign_id": r[0], "status": r[1]} if isinstance(r, tuple)
                else r
                for r in results
            ],
            "error": error,
        }
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


def _load_overnight_results() -> dict | None:
    """Load last overnight results from disk."""
    from stork.conviction import STORK_HOME
    path = STORK_HOME / "last_overnight.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _log(msg: str):
    from stork.conviction import STORK_HOME
    log_path = STORK_HOME / "server.log"
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


def _cleanup():
    """Kill all child processes on shutdown."""
    import subprocess
    # asyncio subprocesses are tracked by the event loop;
    # cancelling the overnight task handles most cleanup.
    global _overnight_task
    if _overnight_task and not _overnight_task.done():
        _overnight_task.cancel()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Stork Campaign Engine — MCP Server")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--max-concurrent", type=int, default=5)
    parser.add_argument("--stdio", action="store_true",
                        help="Run with stdio transport (used by Claude Code MCP)")
    args = parser.parse_args()

    # Update concurrency
    from stork import trace
    trace.MAX_CONCURRENT = args.max_concurrent
    trace._semaphore = asyncio.Semaphore(args.max_concurrent)

    import atexit
    atexit.register(_cleanup)

    if args.stdio:
        # stdio transport — Claude Code spawns this process and talks over stdin/stdout
        mcp.run(transport="stdio")
    else:
        # HTTP transport — standalone server mode
        mcp.settings.port = args.port
        mcp.settings.host = HOST
        print(f"Stork MCP on {HOST}:{args.port}, max {args.max_concurrent} concurrent agents")
        mcp.run(transport="streamable-http")
