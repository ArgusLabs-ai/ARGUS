"""Canary: no value a pipeline produced leaves the machine unless the user opts in.

Each test plants a canary string and fails if it reaches an outbound payload
(or, for ``redact_keys``, the saved run file).
"""

from __future__ import annotations

import json
from typing import TypedDict

import pytest
from conftest import make_run_record

from argus import cloud, llm_proxy, storage

CANARY = "CANARY-SSN-123-45-6789"
CANARY_EMAIL = "canary@leak.test"
CANARY_NODE = "canary_node"


@pytest.fixture
def logged_in(monkeypatch):
    creds = cloud.Credentials("tok", "ref", "uid", CANARY_EMAIL, 9e12)
    monkeypatch.setattr(cloud, "_get_valid_credentials", lambda: creds)
    monkeypatch.setattr(cloud, "is_logged_in", lambda: True)
    monkeypatch.delenv("ARGUS_CLOUD_SYNC", raising=False)
    monkeypatch.delenv("ARGUS_HOSTED_LLM", raising=False)
    sent: list = []
    monkeypatch.setattr(cloud, "_supabase_request", lambda *a, **k: sent.append((a, k)))
    return sent


def test_logged_in_save_run_uploads_nothing_without_opt_in(logged_in, tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "_runs_path", lambda: tmp_path)
    storage.save_run(make_run_record(run_id="r1"))
    assert logged_in == []

    monkeypatch.setenv("ARGUS_CLOUD_SYNC", "1")
    storage.save_run(make_run_record(run_id="r2"))
    assert len(logged_in) == 1  # opted in: the full run goes up, as documented


def test_shared_signature_carries_no_evidence_nodes_or_email(logged_in):
    sig = {
        "id": "sig1",
        "category": "placeholder_outputs",
        "pattern": "lorem ipsum",
        "match_strategy": "contains_ci",
        "severity": "warning",
        "description": "placeholder text",
        "reasoning": f"saw {CANARY} in the output",
        "evidence": [CANARY],
        "source_nodes": [CANARY_NODE],
        "metadata": {"confidence": 0.9, "frequency": 3},
    }
    assert cloud.push_shared_signature(sig)
    payload = json.dumps(logged_in[0][1]["body"])
    for canary in (CANARY, CANARY_EMAIL, CANARY_NODE):
        assert canary not in payload


def test_hosted_llm_proxy_is_opt_in(logged_in, monkeypatch):
    calls: list = []

    def _urlopen(req, **k):
        calls.append(req.data)
        raise OSError("offline")

    monkeypatch.setattr(llm_proxy, "_PROXY_URL", "https://proxy.invalid")
    monkeypatch.setattr(llm_proxy, "SUPABASE_URL", "https://sb.invalid")
    monkeypatch.setattr(llm_proxy, "_get_valid_credentials", cloud._get_valid_credentials)
    monkeypatch.setattr(llm_proxy.urllib.request, "urlopen", _urlopen)

    msg = [{"role": "user", "content": CANARY}]
    assert "error" in llm_proxy._call_proxy(model="gpt-4.1", messages=msg)
    assert calls == []

    monkeypatch.setenv("ARGUS_HOSTED_LLM", "1")
    llm_proxy._call_proxy(model="gpt-4.1", messages=msg)
    assert len(calls) == 1


def test_recorder_redact_keys_keeps_value_out_of_run_file():
    pytest.importorskip("langchain_core")
    from langgraph.graph import END, START, StateGraph

    from argus.recorder import ArgusRecorder

    class S(TypedDict, total=False):
        customer: dict
        answer: str

    def lookup(state: S) -> dict:
        return {"customer": {"ssn": CANARY, "name": "x"}}

    def reply(state: S) -> dict:
        return {"answer": "done"}

    g = StateGraph(S)
    g.add_node("lookup", lookup)
    g.add_node("reply", reply)
    g.add_edge(START, "lookup")
    g.add_edge("lookup", "reply")
    g.add_edge("reply", END)

    recorder = ArgusRecorder(semantic_judge=False, redact_keys={"ssn"})
    recorder.attach(g.compile()).invoke({"customer": {"ssn": CANARY}})
    text = storage.load_run_text(recorder.session.run_id)
    assert CANARY not in text
    assert '"name": "x"' in text  # only the named key is scrubbed
