import json
from types import SimpleNamespace as NS

import pytest

from safechange.models import Status
from safechange.proposer import LLMProposer, ProposerError, RulesProposer

C0 = "/spec/template/spec/containers/0"


@pytest.mark.parametrize("task,ops", [
    ("scale web to 5", [{"op": "replace", "path": "/spec/replicas", "value": 5}]),
    ("scale deployment/web to 3", [{"op": "replace", "path": "/spec/replicas", "value": 3}]),
    ("set image web=ghcr.io/org/web:1.4.2",
     [{"op": "replace", "path": f"{C0}/image", "value": "ghcr.io/org/web:1.4.2"}]),
    ("set image web/web=nginx:1.27", [{"op": "replace", "path": f"{C0}/image", "value": "nginx:1.27"}]),
    ("set env web LOG_LEVEL=debug",
     [{"op": "add", "path": f"{C0}/env", "value": [{"name": "LOG_LEVEL", "value": "debug"}]}]),
    ("set label web tier=frontend", [{"op": "add", "path": "/metadata/labels/tier", "value": "frontend"}]),
    ("set limits web cpu=300m memory=256Mi",
     [{"op": "replace", "path": f"{C0}/resources/limits/cpu", "value": "300m"},
      {"op": "replace", "path": f"{C0}/resources/limits/memory", "value": "256Mi"}]),
])
def test_rules_grammar(env, task, ops):
    change = RulesProposer().propose(task, "demo", env.backends.reader)
    assert change.ops == ops and change.target.name == "web"


def test_rules_unknown_task(env):
    with pytest.raises(ProposerError):
        RulesProposer().propose("delete everything", "demo", env.backends.reader)


def test_proposer_only_gets_reader(env):
    with pytest.raises(AttributeError):
        env.backends.reader.patch_json  # noqa: B018


def tool_resp(target="web", ops=None, rationale="because", stop="tool_use", extra=()):
    ops = ops if ops is not None else [{"op": "replace", "path": "/spec/replicas", "value_json": "4"}]
    blocks = [*extra, NS(type="tool_use", name="propose_patch",
                         input={"target_name": target, "ops": ops, "rationale": rationale})]
    return NS(stop_reason=stop, content=blocks)


class StubClient:
    def __init__(self, resp):
        self.resp, self.calls = resp, []
        self.beta = NS(messages=NS(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        return self.resp


def test_llm_parses_structured_tool_call(env):
    client = StubClient(tool_resp())
    change = LLMProposer(client=client).propose("scale web to 4", "demo", env.backends.reader)
    assert change.ops == [{"op": "replace", "path": "/spec/replicas", "value": 4}]
    kw = client.calls[0]
    assert kw["tools"][0]["strict"] is True and kw["tool_choice"] == {"type": "auto"}
    assert "cluster_objects" in kw["messages"][0]["content"]


def test_llm_free_text_rejected(env):
    resp = NS(stop_reason="end_turn", content=[NS(type="text", text="just run kubectl scale")])
    with pytest.raises(ProposerError, match="free-text"):
        LLMProposer(client=StubClient(resp)).propose("scale", "demo", env.backends.reader)


def test_llm_refusal_rejected(env):
    with pytest.raises(ProposerError, match="refused"):
        LLMProposer(client=StubClient(tool_resp(stop="refusal"))).propose("x", "demo", env.backends.reader)


def test_llm_unknown_target_rejected(env):
    with pytest.raises(ProposerError, match="unknown deployment"):
        LLMProposer(client=StubClient(tool_resp(target="kube-apiserver"))).propose(
            "x", "demo", env.backends.reader)


def test_llm_bad_value_json_rejected(env):
    resp = tool_resp(ops=[{"op": "replace", "path": "/spec/replicas", "value_json": "{nope"}])
    with pytest.raises(ProposerError, match="value_json"):
        LLMProposer(client=StubClient(resp)).propose("x", "demo", env.backends.reader)


def test_llm_two_tool_calls_rejected(env):
    resp = tool_resp(extra=[tool_resp().content[0]])
    with pytest.raises(ProposerError, match="exactly one"):
        LLMProposer(client=StubClient(resp)).propose("x", "demo", env.backends.reader)


def test_llm_malicious_proposal_stopped_by_guardrails(env):
    """A prompt-injected model proposing privilege escalation is caught by policy."""
    resp = tool_resp(ops=[{"op": "add", "path": f"{C0}/securityContext",
                           "value_json": json.dumps({"privileged": True})}])
    p = env.pipeline.propose("scale web", LLMProposer(client=StubClient(resp)), "demo")
    assert p.status == Status.POLICY_FAILED
    assert {"paths_allowlisted", "no_privileged_or_host_access"} <= {
        r.name for r in p.policy_results if not r.passed}


def test_llm_through_full_pipeline(env):
    p = env.pipeline.propose("scale web to 4", LLMProposer(client=StubClient(tool_resp())), "demo")
    assert p.status == Status.PENDING_APPROVAL and p.proposer == "llm"
    env.approve(p)
    assert env.pipeline.apply(p.id, "ops").status == Status.APPLIED
