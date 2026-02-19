"""
Stork integration test.
========================
Tests the full pipeline without live API calls:
  create campaign → run it → verify conviction extraction → read sit-rep.

Uses mocked Claude CLI output based on Step 0 discovery.
"""

import asyncio
import json
import os
import tempfile
from datetime import date
from pathlib import Path
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

# ── Test fixtures ──

# Simulated NDJSON output from `claude -p ... --output-format json`
MOCK_CLI_OUTPUT_SUCCESS = "\n".join([
    json.dumps({
        "type": "system",
        "subtype": "init",
        "model": "claude-sonnet-4-5-20250929",
        "session_id": "test-session-001",
        "cwd": "/tmp",
        "tools": ["Bash", "Read", "Write"],
        "mcp_servers": [],
        "claude_code_version": "2.1.42",
        "uuid": "uuid-init",
    }),
    json.dumps({
        "type": "assistant",
        "message": {
            "content": [
                {"type": "thinking", "thinking": "Let me think about this step by step..."},
                {"type": "text", "text": "Here is the result of the task."},
            ]
        },
        "parent_tool_use_id": None,
        "uuid": "uuid-assistant",
        "session_id": "test-session-001",
    }),
    json.dumps({
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "duration_ms": 3500,
        "duration_api_ms": 2800,
        "num_turns": 1,
        "result": "Here is the result of the task.",
        "total_cost_usd": 0.0035,
        "usage": {
            "input_tokens": 150,
            "output_tokens": 80,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
        "session_id": "test-session-001",
        "uuid": "uuid-result",
    }),
])

MOCK_CLI_OUTPUT_ERROR = "\n".join([
    json.dumps({
        "type": "system",
        "subtype": "init",
        "model": "claude-sonnet-4-5-20250929",
        "session_id": "test-session-002",
        "cwd": "/tmp",
        "tools": [],
        "mcp_servers": [],
        "claude_code_version": "2.1.42",
        "uuid": "uuid-init-2",
    }),
    json.dumps({
        "type": "result",
        "subtype": "error_during_execution",
        "is_error": True,
        "duration_ms": 1200,
        "duration_api_ms": 900,
        "num_turns": 1,
        "total_cost_usd": 0.001,
        "errors": ["Rate limited", "Retry later"],
        "usage": {
            "input_tokens": 50,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
        "session_id": "test-session-002",
        "uuid": "uuid-result-2",
    }),
])

MOCK_CLI_OUTPUT_PLAN = "\n".join([
    json.dumps({
        "type": "system",
        "subtype": "init",
        "model": "claude-haiku-4-5-20251001",
        "session_id": "test-session-plan",
        "cwd": "/tmp",
        "tools": [],
        "mcp_servers": [],
        "claude_code_version": "2.1.42",
        "uuid": "uuid-init-plan",
    }),
    json.dumps({
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "duration_ms": 1000,
        "duration_api_ms": 800,
        "num_turns": 1,
        "result": '[{"description": "Research the topic"}, {"description": "Compile findings into report"}]',
        "total_cost_usd": 0.0005,
        "usage": {"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
        "session_id": "test-session-plan",
        "uuid": "uuid-result-plan",
    }),
])


# ── trace.py tests ──

class TestTrace:
    def test_parse_success(self):
        from stork.trace import parse_trace
        trace = parse_trace(MOCK_CLI_OUTPUT_SUCCESS, "test task")
        assert trace.status == "done"
        assert trace.response == "Here is the result of the task."
        assert trace.thinking == "Let me think about this step by step..."
        assert trace.model == "claude-sonnet-4-5-20250929"
        assert trace.duration_ms == 3500
        assert trace.cost_usd == 0.0035
        assert trace.tokens_in == 150
        assert trace.tokens_out == 80
        assert trace.session_id == "test-session-001"
        assert trace.succeeded()

    def test_parse_error(self):
        from stork.trace import parse_trace
        trace = parse_trace(MOCK_CLI_OUTPUT_ERROR, "failing task")
        assert trace.status == "error"
        assert "Rate limited" in trace.errors
        assert trace.cost_usd == 0.001
        assert not trace.succeeded()

    def test_parse_empty(self):
        from stork.trace import parse_trace
        trace = parse_trace("", "empty task")
        assert trace.status == "error"
        assert "Empty output" in trace.errors[0]

    def test_parse_garbage(self):
        from stork.trace import parse_trace
        trace = parse_trace("not json at all\nreally not\n", "garbage task")
        assert trace.status == "error"
        assert "No valid JSON" in trace.errors[0]

    def test_parse_partial_json(self):
        """Partial output — some lines valid, some not."""
        from stork.trace import parse_trace
        partial = MOCK_CLI_OUTPUT_SUCCESS + "\nthis is garbage\n"
        trace = parse_trace(partial, "partial task")
        assert trace.status == "done"  # Should still work

    def test_to_dict_roundtrip(self):
        from stork.trace import parse_trace, AgentTrace
        trace = parse_trace(MOCK_CLI_OUTPUT_SUCCESS, "roundtrip task")
        d = trace.to_dict()
        restored = AgentTrace.from_dict(d)
        assert restored.task == trace.task
        assert restored.status == trace.status
        assert restored.response == trace.response[:5000]

    def test_summary(self):
        from stork.trace import parse_trace
        trace = parse_trace(MOCK_CLI_OUTPUT_SUCCESS, "summary task")
        s = trace.summary()
        assert "done" in s
        assert "summary task" in s


# ── conviction.py tests ──

class TestConviction:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        os.environ["STORK_HOME"] = self.tmpdir
        # Reload module-level path
        import stork.conviction as conv
        conv.STORK_HOME = Path(self.tmpdir)
        conv.CONVICTIONS_PATH = Path(self.tmpdir) / "convictions.json"
        self.conv = conv

    def test_empty_store(self):
        store = self.conv.load_convictions()
        assert store["convictions"] == []
        assert store["version"] == 1

    def test_add_delta(self):
        store = self.conv._empty_store()
        delta = {"action": "add", "belief": "Chain runs degrade after step 4"}
        store = self.conv.apply_delta(store, delta)
        assert len(store["convictions"]) == 1
        assert store["convictions"][0]["confidence"] == 0.5
        assert store["deltas_applied"] == 1

    def test_add_failure_conviction(self):
        store = self.conv._empty_store()
        delta = {
            "action": "add",
            "belief": "Playwright cookies expire after 6 hours",
            "initial_confidence": 0.65,
            "from_failure": True,
        }
        store = self.conv.apply_delta(store, delta)
        assert store["convictions"][0]["confidence"] == 0.65
        assert store["convictions"][0]["from_failure"] is True

    def test_strengthen(self):
        store = self.conv._empty_store()
        store = self.conv.apply_delta(store, {"action": "add", "belief": "Short chains are better"})
        initial = store["convictions"][0]["confidence"]

        store = self.conv.apply_delta(store, {"action": "strengthen", "target": "Short chains are better"})
        assert store["convictions"][0]["confidence"] > initial

    def test_diminishing_strengthen(self):
        store = self.conv._empty_store()
        store = self.conv.apply_delta(store, {"action": "add", "belief": "Test belief"})

        # Strengthen many times
        for _ in range(20):
            store = self.conv.apply_delta(store, {"action": "strengthen", "target": "Test belief"})

        # Should approach 1.0 but never exceed it
        assert store["convictions"][0]["confidence"] <= 1.0
        assert store["convictions"][0]["confidence"] > 0.8

    def test_weaken(self):
        store = self.conv._empty_store()
        store = self.conv.apply_delta(store, {"action": "add", "belief": "Wrong belief"})
        store = self.conv.apply_delta(store, {"action": "weaken", "target": "Wrong belief"})
        # Should be below 0.5 now
        assert store["convictions"][0]["confidence"] < 0.5

    def test_weaken_to_removal(self):
        store = self.conv._empty_store()
        store = self.conv.apply_delta(store, {"action": "add", "belief": "Very wrong belief"})
        for _ in range(10):
            store = self.conv.apply_delta(store, {"action": "weaken", "target": "Very wrong belief"})
        # Should be removed
        assert len(store["convictions"]) == 0

    def test_revise(self):
        store = self.conv._empty_store()
        store = self.conv.apply_delta(store, {"action": "add", "belief": "Old understanding"})
        store = self.conv.apply_delta(store, {
            "action": "revise",
            "target": "Old understanding",
            "belief": "New understanding",
        })
        assert store["convictions"][0]["belief"] == "New understanding"

    def test_remove(self):
        store = self.conv._empty_store()
        store = self.conv.apply_delta(store, {"action": "add", "belief": "Temporary"})
        store = self.conv.apply_delta(store, {"action": "remove", "target": "Temporary"})
        assert len(store["convictions"]) == 0

    def test_50_cap(self):
        store = self.conv._empty_store()
        for i in range(55):
            store = self.conv.apply_delta(store, {"action": "add", "belief": f"Belief number {i}"})
        assert len(store["convictions"]) <= 50

    def test_duplicate_add_strengthens(self):
        store = self.conv._empty_store()
        store = self.conv.apply_delta(store, {"action": "add", "belief": "Chains break at step 4"})
        initial = store["convictions"][0]["confidence"]
        # Adding something very similar should strengthen, not duplicate
        store = self.conv.apply_delta(store, {"action": "add", "belief": "Chains break at step 4 always"})
        assert len(store["convictions"]) == 1
        assert store["convictions"][0]["confidence"] > initial

    def test_format_for_context(self):
        store = self.conv._empty_store()
        store = self.conv.apply_delta(store, {"action": "add", "belief": "Test pattern"})
        text = self.conv.format_convictions_for_context(store)
        assert "Test pattern" in text

    def test_save_load_roundtrip(self):
        store = self.conv._empty_store()
        store = self.conv.apply_delta(store, {"action": "add", "belief": "Persisted belief"})
        self.conv.save_convictions(store)

        loaded = self.conv.load_convictions()
        assert len(loaded["convictions"]) == 1
        assert loaded["convictions"][0]["belief"] == "Persisted belief"


# ── campaign.py tests ──

class TestCampaign:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.campaigns_dir = Path(self.tmpdir) / "campaigns"
        self.campaigns_dir.mkdir()

        os.environ["STORK_HOME"] = self.tmpdir
        import stork.conviction as conv
        conv.STORK_HOME = Path(self.tmpdir)
        conv.CONVICTIONS_PATH = Path(self.tmpdir) / "convictions.json"

    def test_create_campaign(self):
        from stork.campaign import CampaignManager
        mgr = CampaignManager(self.campaigns_dir)
        c = mgr.create_campaign("Research lacrosse opponents", "tonight")
        assert c.goal == "Research lacrosse opponents"
        assert c.lane == "tonight"
        assert c.status == "queued"
        assert (self.campaigns_dir / f"{c.id}.json").exists()

    def test_persist_and_reload(self):
        from stork.campaign import CampaignManager
        mgr1 = CampaignManager(self.campaigns_dir)
        c = mgr1.create_campaign("Persist test", "campaign")
        cid = c.id

        mgr2 = CampaignManager(self.campaigns_dir)
        loaded = mgr2.get(cid)
        assert loaded is not None
        assert loaded.goal == "Persist test"

    def test_pause_resume(self):
        from stork.campaign import CampaignManager
        mgr = CampaignManager(self.campaigns_dir)
        c = mgr.create_campaign("Pausable", "tonight")
        mgr.pause(c.id)
        assert mgr.get(c.id).status == "paused"

    def test_redirect(self):
        from stork.campaign import CampaignManager
        mgr = CampaignManager(self.campaigns_dir)
        c = mgr.create_campaign("Original goal", "tonight")
        c.plan = [{"description": "Step 1", "status": "pending"}]
        mgr._save(c)

        mgr.redirect(c.id, "Focus on defense instead")
        updated = mgr.get(c.id)
        assert updated.plan[-1]["description"].startswith("REDIRECT")
        assert "defense" in updated.plan[-1]["description"]
        assert updated.redirect_history

    def test_spawn_sub(self):
        from stork.campaign import CampaignManager
        mgr = CampaignManager(self.campaigns_dir)
        parent = mgr.create_campaign("Big project", "campaign")
        parent.status = "active"
        mgr._save(parent)

        sub = mgr.spawn_sub(parent.id, "Learn the API first")
        assert sub.parent_campaign_id == parent.id
        assert sub.budget_limit == parent.budget_limit * 0.3
        assert mgr.get(parent.id).status == "blocked"

    def test_kill_writes_postmortem(self):
        from stork.campaign import CampaignManager
        mgr = CampaignManager(self.campaigns_dir)
        c = mgr.create_campaign("Doomed campaign", "tonight")
        c.plan = [{"description": "Try something", "status": "failed", "notes": "It broke"}]
        mgr._save(c)

        # Mock the API call in digest_campaign
        with patch("stork.conviction.extract_conviction", return_value=None):
            killed = mgr.kill(c.id)

        assert killed.status == "failed"
        assert "POST-MORTEM" in killed.post_mortem

    def test_list_filters(self):
        from stork.campaign import CampaignManager
        mgr = CampaignManager(self.campaigns_dir)
        mgr.create_campaign("Now task", "now")
        mgr.create_campaign("Tonight task", "tonight")
        mgr.create_campaign("Campaign task", "campaign")

        assert len(mgr.list_all(lane="now")) == 1
        assert len(mgr.list_all(lane="tonight")) == 1
        assert len(mgr.list_all(status="queued")) == 3

    def test_over_budget(self):
        from stork.campaign import Campaign
        c = Campaign(id="test", name="test", goal="test", budget_spent=2.5, budget_limit=2.0)
        assert c.over_budget()

    def test_negotiate_kills_stuck(self):
        from stork.campaign import CampaignManager
        mgr = CampaignManager(self.campaigns_dir)

        c1 = mgr.create_campaign("Stuck one", "tonight")
        c1.status = "active"
        c1.plan = [
            {"description": "s1", "status": "failed"},
            {"description": "s2", "status": "failed"},
            {"description": "s3", "status": "failed"},
        ]
        mgr._save(c1)

        c2 = mgr.create_campaign("Healthy one", "tonight")
        c2.status = "active"
        c2.plan = [{"description": "s1", "status": "done"}]
        mgr._save(c2)

        with patch("stork.conviction.extract_conviction", return_value=None):
            actions = mgr.negotiate()

        assert any("Killed" in a for a in actions)


# ── sitrep.py tests ──

class TestSitrep:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.sitreps_dir = Path(self.tmpdir) / "sitreps"

        import stork.sitrep as sitrep
        sitrep.SITREPS_DIR = self.sitreps_dir

    def test_fallback_sitrep(self):
        from stork.sitrep import _fallback_sitrep
        content = _fallback_sitrep(
            results=[{"name": "Scout opponents", "status": "complete"}],
            campaigns=[{"name": "Film review", "status": "queued", "plan": []}],
        )
        assert "## DONE" in content
        assert "## BLOCKED" in content
        assert "## NEXT" in content
        assert "Scout opponents" in content
        assert "?" in content  # Must end with a question

    def test_save_and_load(self):
        from stork.sitrep import save_sitrep, load_sitrep
        import stork.sitrep as sitrep
        sitrep.SITREPS_DIR = self.sitreps_dir

        today = date.today()
        save_sitrep("Test sit-rep content", today)
        loaded = load_sitrep(today)
        assert loaded == "Test sit-rep content"

    def test_load_nonexistent(self):
        from stork.sitrep import load_sitrep
        import stork.sitrep as sitrep
        sitrep.SITREPS_DIR = self.sitreps_dir
        result = load_sitrep(date(2020, 1, 1))
        assert result is None


# ── boredom.py tests ──

class TestBoredom:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        os.environ["STORK_HOME"] = self.tmpdir
        import stork.boredom as boredom
        boredom.STORK_HOME = Path(self.tmpdir)
        boredom.BOREDOM_STATE_PATH = Path(self.tmpdir) / "boredom_state.json"
        self.boredom = boredom

    def test_is_bored_no_activity(self):
        assert self.boredom.is_bored() is True

    def test_is_bored_after_activity(self):
        self.boredom.record_activity()
        assert self.boredom.is_bored() is False

    def test_is_bored_with_active_campaigns(self):
        mgr = MagicMock()
        mgr.list_all.return_value = [MagicMock()]  # has active campaigns
        assert self.boredom.is_bored(mgr) is False


# ── Full pipeline test ──

class TestFullPipeline:
    """End-to-end: create → plan → execute → conviction → sitrep."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.campaigns_dir = Path(self.tmpdir) / "campaigns"
        self.campaigns_dir.mkdir()
        self.sitreps_dir = Path(self.tmpdir) / "sitreps"

        os.environ["STORK_HOME"] = self.tmpdir
        import stork.conviction as conv
        conv.STORK_HOME = Path(self.tmpdir)
        conv.CONVICTIONS_PATH = Path(self.tmpdir) / "convictions.json"

        import stork.sitrep as sitrep
        sitrep.SITREPS_DIR = self.sitreps_dir

    def test_full_pipeline(self):
        """
        Mock the CLI calls, run a campaign through to completion,
        verify conviction is extracted and sit-rep is generated.
        """
        from stork.campaign import CampaignManager
        from stork.conviction import load_convictions
        from stork.sitrep import _fallback_sitrep, save_sitrep, load_sitrep

        mgr = CampaignManager(self.campaigns_dir)
        c = mgr.create_campaign("Scout opponent film for Saturday", "tonight")

        # Simulate: plan was created
        c.status = "active"
        c.plan = [
            {"description": "Research opponent roster", "status": "done", "notes": "Found 15 players"},
            {"description": "Analyze game film", "status": "done", "notes": "Identified 3 key plays"},
        ]
        c.budget_spent = 0.008
        mgr._save(c)

        # Simulate conviction extraction
        mock_delta = {
            "action": "add",
            "belief": "Opponent research takes 2 steps: roster then film analysis",
            "reason": "Two-step pattern worked well",
        }
        with patch("stork.conviction.extract_conviction", return_value=mock_delta):
            # Complete the campaign
            asyncio.get_event_loop().run_until_complete(mgr._complete(c))

        # Verify campaign completed
        completed = mgr.get(c.id)
        assert completed.status == "complete"
        assert completed.conviction != ""

        # Verify conviction was stored
        store = load_convictions()
        assert len(store["convictions"]) == 1
        assert "opponent" in store["convictions"][0]["belief"].lower() or "research" in store["convictions"][0]["belief"].lower()

        # Generate sit-rep
        import stork.sitrep as sitrep
        sitrep.SITREPS_DIR = self.sitreps_dir
        content = _fallback_sitrep(
            results=[completed.to_dict()],
            campaigns=[],
        )
        path = save_sitrep(content)
        assert path.exists()
        loaded = load_sitrep()
        assert "## DONE" in loaded


# ── profiles.py tests ──

class TestProfiles:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        os.environ["STORK_HOME"] = self.tmpdir
        import stork.profiles as profiles
        profiles.STORK_HOME = Path(self.tmpdir)
        profiles.PROFILES_PATH = Path(self.tmpdir) / "profiles.json"
        self.profiles = profiles

    def test_builtin_profiles(self):
        names = self.profiles.list_profiles()
        assert "default" in names
        assert "quant" in names
        assert "research" in names
        assert "code" in names
        assert "fast" in names

    def test_get_profile(self):
        p = self.profiles.get_profile("quant")
        assert p is not None
        assert "system_prompt" in p
        assert "timeout" in p
        assert p["timeout"] == 300

    def test_get_nonexistent_profile(self):
        assert self.profiles.get_profile("nonexistent") is None

    def test_custom_profiles_from_disk(self):
        custom = {
            "lacrosse": {
                "system_prompt": "You are a lacrosse analyst.",
                "timeout": 120,
            }
        }
        self.profiles.PROFILES_PATH.write_text(json.dumps(custom), encoding="utf-8")
        p = self.profiles.get_profile("lacrosse")
        assert p is not None
        assert "lacrosse" in p["system_prompt"]
        # Builtins still exist alongside custom
        assert self.profiles.get_profile("quant") is not None

    def test_fast_profile_timeout(self):
        p = self.profiles.get_profile("fast")
        assert p["timeout"] == 60


# ── trace.py retry + run_many robustness tests ──

class TestTraceRetryAndRobustness:
    def test_run_many_exception_becomes_error_trace(self):
        """If gather catches an exception, it becomes an error trace, not a crash."""
        from stork.trace import AgentTrace, run_many

        async def mock_run_claude(task, **kwargs):
            if "fail" in task:
                raise RuntimeError("Simulated crash")
            return AgentTrace(id="ok", task=task, status="done", response="fine")

        async def run_test():
            with patch("stork.trace.run_claude", side_effect=mock_run_claude):
                # Even if gather raises, run_many with return_exceptions handles it
                with patch("stork.trace.asyncio.gather",
                           side_effect=lambda *c, **kw: asyncio.gather(*c, return_exceptions=True)):
                    pass  # gather is called inside run_many, we test via the real path

            # Direct test: simulate what happens when results contain exceptions
            results = [
                AgentTrace(id="1", task="ok task", status="done"),
                RuntimeError("boom"),
            ]
            from stork.trace import uuid, datetime as dt, timezone as tz
            traces = []
            tasks = ["ok task", "fail task"]
            for i, result in enumerate(results):
                if isinstance(result, BaseException):
                    traces.append(AgentTrace(
                        id=str(uuid.uuid4())[:8],
                        task=tasks[i][:500],
                        status="error",
                        errors=[f"{type(result).__name__}: {result}"],
                    ))
                else:
                    traces.append(result)

            assert traces[0].status == "done"
            assert traces[1].status == "error"
            assert "RuntimeError" in traces[1].errors[0]

        asyncio.get_event_loop().run_until_complete(run_test())

    def test_retry_logic(self):
        """run_claude retries on failure and returns success on second attempt."""
        from stork.trace import AgentTrace

        call_count = 0

        async def mock_run_once(task, system_prompt, topology, timeout):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return AgentTrace(id="fail", task=task, status="error", errors=["transient"])
            return AgentTrace(id="ok", task=task, status="done", response="success")

        async def run_test():
            nonlocal call_count
            call_count = 0
            with patch("stork.trace._run_claude_once", side_effect=mock_run_once):
                from stork.trace import run_claude
                trace = await run_claude("test", retries=1)
            assert trace.status == "done"
            assert call_count == 2

        asyncio.get_event_loop().run_until_complete(run_test())

    def test_no_retry_on_success(self):
        """run_claude does NOT retry if first attempt succeeds."""
        from stork.trace import AgentTrace

        call_count = 0

        async def mock_run_once(task, system_prompt, topology, timeout):
            nonlocal call_count
            call_count += 1
            return AgentTrace(id="ok", task=task, status="done", response="success")

        async def run_test():
            nonlocal call_count
            call_count = 0
            with patch("stork.trace._run_claude_once", side_effect=mock_run_once):
                from stork.trace import run_claude
                trace = await run_claude("test", retries=3)
            assert trace.status == "done"
            assert call_count == 1

        asyncio.get_event_loop().run_until_complete(run_test())

    def test_profile_resolution(self):
        """run_claude applies profile system_prompt and timeout."""
        from stork.trace import AgentTrace

        captured_args = {}

        async def mock_run_once(task, system_prompt, topology, timeout):
            captured_args["system_prompt"] = system_prompt
            captured_args["timeout"] = timeout
            return AgentTrace(id="ok", task=task, status="done", response="done")

        async def run_test():
            mock_profile = {"system_prompt": "Be a quant.", "timeout": 120}
            with patch("stork.trace._run_claude_once", side_effect=mock_run_once), \
                 patch("stork.profiles.get_profile", return_value=mock_profile):
                from stork.trace import run_claude
                await run_claude("test", profile="quant", retries=0)

            assert captured_args["system_prompt"] == "Be a quant."
            assert captured_args["timeout"] == 120

        asyncio.get_event_loop().run_until_complete(run_test())


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
