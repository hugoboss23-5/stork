"""
Stork Trace — The nervous system.
==================================
Replaces agent_army's run_claude() with --output-format json parsing.
Returns AgentTrace: thinking, response, duration, tokens, errors, task, topology_position.
A failed run IS a trace. Timeout is data. Empty output is data. Never crash on garbage.

Hugo & Watty · February 2026
"""

import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

CLAUDE_CMD = os.environ.get("STORK_CLAUDE_CMD", "claude")
TIMEOUT = int(os.environ.get("STORK_TIMEOUT", "300"))
MAX_CONCURRENT = int(os.environ.get("STORK_MAX_CONCURRENT", "5"))

_semaphore: asyncio.Semaphore | None = None


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    return _semaphore


@dataclass
class AgentTrace:
    """One agent call, successful or not. The atom of Stork's memory."""

    id: str
    task: str
    thinking: str = ""
    response: str = ""
    duration_ms: int = 0
    duration_api_ms: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cache_read: int = 0
    cache_create: int = 0
    cost_usd: float = 0.0
    num_turns: int = 0
    model: str = ""
    errors: list[str] = field(default_factory=list)
    status: str = "pending"  # pending | queued | running | done | error | timeout
    topology_position: str = ""  # e.g. "chain:2/5", "parallel:3", "solo"
    session_id: str = ""
    started_at: str = ""
    finished_at: str = ""
    raw_result: dict = field(default_factory=dict)

    def succeeded(self) -> bool:
        return self.status == "done" and not self.errors

    def summary(self, max_len: int = 200) -> str:
        """One-line summary for sit-reps and conviction extraction."""
        resp = self.response[:max_len] if self.response else "(no response)"
        if self.errors:
            return f"[{self.status}] {self.task[:80]} -> ERROR: {'; '.join(self.errors[:2])}"
        return f"[{self.status}] {self.task[:80]} -> {resp}"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "task": self.task,
            "thinking": self.thinking,
            "response": self.response[:5000],
            "duration_ms": self.duration_ms,
            "cost_usd": self.cost_usd,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "num_turns": self.num_turns,
            "model": self.model,
            "errors": self.errors,
            "status": self.status,
            "topology_position": self.topology_position,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }

    @staticmethod
    def from_dict(d: dict) -> "AgentTrace":
        return AgentTrace(
            id=d.get("id", ""),
            task=d.get("task", ""),
            thinking=d.get("thinking", ""),
            response=d.get("response", ""),
            duration_ms=d.get("duration_ms", 0),
            cost_usd=d.get("cost_usd", 0.0),
            tokens_in=d.get("tokens_in", 0),
            tokens_out=d.get("tokens_out", 0),
            num_turns=d.get("num_turns", 0),
            model=d.get("model", ""),
            errors=d.get("errors", []),
            status=d.get("status", "unknown"),
            topology_position=d.get("topology_position", ""),
            started_at=d.get("started_at", ""),
            finished_at=d.get("finished_at", ""),
        )


def _parse_ndjson(raw_output: str) -> list[dict]:
    """Parse newline-delimited JSON. Skip garbage lines. Never crash."""
    objects = []
    for line in raw_output.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            objects.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return objects


def _extract_thinking(messages: list[dict]) -> str:
    """Pull thinking blocks from intermediate assistant messages."""
    thinking_parts = []
    for msg in messages:
        if msg.get("type") != "assistant":
            continue
        message_obj = msg.get("message", {})
        if not isinstance(message_obj, dict):
            continue
        content = message_obj.get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "thinking":
                text = block.get("thinking", "") or block.get("text", "")
                if text:
                    thinking_parts.append(text)
    return "\n---\n".join(thinking_parts)


def _extract_init_metadata(messages: list[dict]) -> dict:
    """Pull model and session info from the system init message."""
    for msg in messages:
        if msg.get("type") == "system" and msg.get("subtype") == "init":
            return {
                "model": msg.get("model", ""),
                "session_id": msg.get("session_id", ""),
            }
    return {}


def _parse_result(messages: list[dict]) -> dict:
    """Find the final result message."""
    for msg in reversed(messages):
        if msg.get("type") == "result":
            return msg
    return {}


def parse_trace(raw_output: str, task: str, trace_id: str = "",
                started_at: str = "", topology: str = "solo") -> AgentTrace:
    """
    Parse raw NDJSON output from claude CLI into an AgentTrace.
    Handles malformed output, missing fields, empty responses. Never crashes.
    """
    tid = trace_id or str(uuid.uuid4())[:8]
    trace = AgentTrace(
        id=tid,
        task=task,
        started_at=started_at or datetime.now(timezone.utc).isoformat(),
        topology_position=topology,
    )

    if not raw_output or not raw_output.strip():
        trace.status = "error"
        trace.errors = ["Empty output from CLI"]
        trace.finished_at = datetime.now(timezone.utc).isoformat()
        return trace

    messages = _parse_ndjson(raw_output)
    if not messages:
        trace.status = "error"
        trace.errors = ["No valid JSON lines in output"]
        trace.finished_at = datetime.now(timezone.utc).isoformat()
        return trace

    # Extract metadata from init message
    init = _extract_init_metadata(messages)
    trace.model = init.get("model", "")
    trace.session_id = init.get("session_id", "")

    # Extract thinking from intermediate messages
    trace.thinking = _extract_thinking(messages)

    # Extract result from final message
    result = _parse_result(messages)
    trace.raw_result = result

    if not result:
        trace.status = "error"
        trace.errors = ["No result message in output"]
        trace.finished_at = datetime.now(timezone.utc).isoformat()
        return trace

    is_error = result.get("is_error", False)
    subtype = result.get("subtype", "")

    trace.response = result.get("result", "")
    trace.duration_ms = result.get("duration_ms", 0)
    trace.duration_api_ms = result.get("duration_api_ms", 0)
    trace.cost_usd = result.get("total_cost_usd", 0.0)
    trace.num_turns = result.get("num_turns", 0)

    # Token usage
    usage = result.get("usage", {})
    if isinstance(usage, dict):
        trace.tokens_in = usage.get("input_tokens", 0)
        trace.tokens_out = usage.get("output_tokens", 0)
        trace.cache_read = usage.get("cache_read_input_tokens", 0)
        trace.cache_create = usage.get("cache_creation_input_tokens", 0)

    if is_error:
        trace.status = "error"
        trace.errors = result.get("errors", [f"Error subtype: {subtype}"])
    else:
        trace.status = "done"

    trace.finished_at = datetime.now(timezone.utc).isoformat()
    return trace


async def run_claude(task: str, system_prompt: str | None = None,
                     topology: str = "solo", timeout: int | None = None) -> AgentTrace:
    """
    Spawn one Claude Code CLI instance with --output-format json.
    Returns AgentTrace — always, even on failure/timeout/garbage.
    """
    trace_id = str(uuid.uuid4())[:8]
    started_at = datetime.now(timezone.utc).isoformat()
    timeout = timeout or TIMEOUT

    sem = _get_semaphore()

    trace = AgentTrace(
        id=trace_id,
        task=task[:500],
        status="queued",
        started_at=started_at,
        topology_position=topology,
    )

    async with sem:
        trace.status = "running"
        try:
            cmd = [CLAUDE_CMD, "-p", task, "--output-format", "json"]
            if system_prompt:
                cmd.extend(["--system-prompt", system_prompt])

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "CLAUDECODE": ""},  # Clear nesting guard
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                trace.status = "timeout"
                trace.errors = [f"Timed out after {timeout}s"]
                trace.finished_at = datetime.now(timezone.utc).isoformat()
                return trace

            raw = stdout.decode("utf-8", errors="replace")
            err = stderr.decode("utf-8", errors="replace").strip()

            parsed = parse_trace(raw, task, trace_id, started_at, topology)

            # If proc failed but we got no error from parsing, capture stderr
            if proc.returncode != 0 and not parsed.errors:
                parsed.errors = [err or f"Exit code {proc.returncode}"]
                parsed.status = "error"

            return parsed

        except FileNotFoundError:
            trace.status = "error"
            trace.errors = [f"Claude CLI not found at: {CLAUDE_CMD}"]
            trace.finished_at = datetime.now(timezone.utc).isoformat()
            return trace
        except Exception as e:
            trace.status = "error"
            trace.errors = [str(e)]
            trace.finished_at = datetime.now(timezone.utc).isoformat()
            return trace


async def run_many(tasks: list[str], system_prompt: str | None = None) -> list[AgentTrace]:
    """Parallel spawn. Each gets a topology position."""
    n = len(tasks)
    coros = [
        run_claude(t, system_prompt=system_prompt, topology=f"parallel:{i+1}/{n}")
        for i, t in enumerate(tasks)
    ]
    return await asyncio.gather(*coros)


async def run_chain(steps: list[str], system_prompt: str | None = None) -> list[AgentTrace]:
    """Sequential pipeline. Each step gets previous output as context."""
    traces = []
    context = ""
    n = len(steps)

    for i, step in enumerate(steps):
        if context:
            full_task = f"PREVIOUS STEP OUTPUT:\n{context}\n\nYOUR TASK (step {i+1}/{n}): {step}"
        else:
            full_task = f"YOUR TASK (step {i+1}/{n}): {step}"

        trace = await run_claude(full_task, system_prompt=system_prompt,
                                 topology=f"chain:{i+1}/{n}")
        traces.append(trace)

        if trace.succeeded():
            context = trace.response
        else:
            # Chain broken — still return what we have
            break

    return traces
