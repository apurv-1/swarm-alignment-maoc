import json
import os
import signal
import threading
import time
from types import SimpleNamespace

import pytest
from adaption._models import construct_type
from adaption.types.response import Response

from monitor import (
    CONDITIONS,
    FRAMING_NOTE,
    build_jobs,
    call_monitor,
    done_keys,
    estimate,
    load_results,
    parse_output,
    plan_calls,
    run,
)
from scenarios import load_all

SCENARIOS = load_all()
MODEL = "deepseek-v4-flash"
REPLY = "<reasoning>r</reasoning><issue>leak</issue><score>72</score>"


def sdk_response(text=REPLY, status="completed", usage=True, cost=0.001, output_tokens=20, error=None, incomplete=None):
    """A Response built with the SDK's own types, the way the client builds one from JSON."""
    payload = {
        "id": "resp_1", "object": "response", "status": status, "model": MODEL, "output_text": text,
        "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
        "error": error, "incomplete_details": incomplete,
    }
    if usage:
        payload["usage"] = {
            "input_tokens": 100, "output_tokens": output_tokens, "total_tokens": 100 + output_tokens,
            "output_tokens_details": {"reasoning_tokens": 5},
        }
        if cost is not None:
            payload["usage"]["cost"] = cost
    return construct_type(value=payload, type_=Response)


class FakeClient:
    """Stands in for the Adaption client so tests never call the API."""

    def __init__(self, reply=REPLY, fail=None, delay=0.0, **response_options):
        self.calls = []
        self.reply = reply
        self.fail = fail
        self.delay = delay
        self.response_options = response_options
        self.responses = self
        self.lock = threading.Lock()

    def create(self, **kwargs):
        with self.lock:
            self.calls.append(kwargs)
        time.sleep(self.delay)
        if self.fail:
            raise self.fail
        return sdk_response(self.reply, **self.response_options)


def _records(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def _calls(scenarios=SCENARIOS[:1], conditions=("solo",), readings=1, max_output_tokens=100, temperature=1.0):
    jobs = build_jobs(scenarios, conditions)
    return jobs, plan_calls(jobs, [MODEL], readings, temperature, max_output_tokens)


def test_parse_output_takes_last_valid_score_and_last_issue():
    score, issue = parse_output("<issue>first</issue><score>10</score> then <issue> second </issue><score>85</score>")
    assert score == 85 and issue == "second"
    assert parse_output("<score>**40**</score>")[0] == 40
    assert parse_output("no score")[0] is None
    assert parse_output("<score>150</score>")[0] is None


def test_build_jobs_counts_parts_and_frames_whole_log_views_only():
    scenario = SCENARIOS[0]
    jobs = build_jobs([scenario], CONDITIONS, framings=(False, True))
    per_agent_jobs = [j for j in jobs if j.condition == "per_agent"]
    assert len(per_agent_jobs) == len(scenario.agents)
    assert not any(j.framing for j in per_agent_jobs)
    assert len([j for j in jobs if j.condition != "per_agent"]) == (len(CONDITIONS) - 1) * 2
    # Duplicated conditions, even from a generator, do not duplicate jobs.
    assert len(build_jobs([scenario], (c for c in ["solo", "solo", "per_agent"]))) == 1 + len(scenario.agents)


def test_framing_changes_instructions_only_and_instructions_are_neutral():
    plain, framed = build_jobs(SCENARIOS[:1], ["solo"], framings=(False, True))
    assert plain.transcript == framed.transcript
    assert FRAMING_NOTE in framed.instructions and FRAMING_NOTE not in plain.instructions
    for word in ("agent", "team", "test data", "leak"):
        assert word not in plain.instructions.lower()
    assert framed.instructions.index(FRAMING_NOTE) < framed.instructions.index("Answer in this format")


def test_identical_prompts_share_readings_and_settings_change_keys():
    jobs = build_jobs(SCENARIOS, ["per_agent"])
    calls = plan_calls(jobs, [MODEL, MODEL], 2, 1.0, 100)
    assert len(calls) == 2 * len({j.prompt_hash for j in jobs}) < 2 * len(jobs)
    job = jobs[0]
    keys = {job.key(MODEL, 1.0, 100, 0), job.key(MODEL, 0.7, 100, 0), job.key(MODEL, 1.0, 200, 0), job.key(MODEL, 1.0, 100, 1)}
    assert len(keys) == 4


def test_calls_are_reading_major():
    _, calls = _calls(SCENARIOS[:2], readings=3)
    readings = [c.reading for c in calls.values()]
    assert readings == sorted(readings)


def test_estimate_has_an_upper_bound_and_scales():
    _, one = _calls(SCENARIOS[:2], readings=1, max_output_tokens=4000)
    _, five = _calls(SCENARIOS[:2], readings=5, max_output_tokens=4000)
    e1, e5 = estimate(one.values())[MODEL], estimate(five.values())[MODEL]
    assert e5["calls"] == 5 * e1["calls"] and e5["credits"] > e1["credits"]
    assert e1["max_credits"] > e1["credits"]


def test_run_records_results_and_resumes_without_repeating(tmp_path):
    out = tmp_path / "r.jsonl"
    jobs, calls = _calls(SCENARIOS[:2], ("solo", "per_agent"), readings=2, temperature=0.7)
    client = FakeClient()
    first = run(client, calls, out, workers=2)
    assert first["written"] == len(client.calls) == len(calls)
    assert all(call["temperature"] == 0.7 and call["max_output_tokens"] == 100 for call in client.calls)
    records = _records(out)
    assert len({r["key"] for r in records}) == len(records)
    assert all(r["score"] == 72 and r["issue"] == "leak" and r["cost"] == 0.001 for r in records)
    assert all(r["response_model"] == MODEL and r["status"] == "completed" for r in records)
    assert records[0]["output_tokens_details"] == {"reasoning_tokens": 5}
    assert run(client, calls, out, workers=2)["calls"] == 0 and len(client.calls) == first["written"]


def test_connection_errors_are_retried_but_timeouts_only_on_request(tmp_path):
    out = tmp_path / "r.jsonl"
    _, calls = _calls()
    run(FakeClient(fail=ConnectionError("down")), calls, out, workers=1)
    assert _records(out)[0]["error_kind"] == "other" and done_keys(out) == set()
    assert run(FakeClient(), calls, out, workers=1)["calls"] == 1

    out2 = tmp_path / "t.jsonl"
    run(FakeClient(fail=TimeoutError("slow")), calls, out2, workers=1)
    assert _records(out2)[0]["error_kind"] == "timeout" and _records(out2)[0]["score"] is None
    assert run(FakeClient(), calls, out2, workers=1)["calls"] == 0
    assert run(FakeClient(), calls, out2, workers=1, retry_timeouts=True)["calls"] == 1


@pytest.mark.parametrize("options", [{"usage": False}, {"cost": None}], ids=["no-usage", "no-cost-field"])
def test_missing_usage_or_cost_keeps_the_paid_reply(options):
    jobs, calls = _calls()
    record = call_monitor(FakeClient(**options), next(iter(calls.values())))
    assert record["error"] is None and record["score"] == 72
    json.dumps(record, allow_nan=False)
    if options.get("cost", 1) is None:
        assert record["cost"] > 0  # computed from token counts


def test_failed_and_truncated_replies_keep_their_evidence():
    _, calls = _calls()
    call = next(iter(calls.values()))
    failed = call_monitor(FakeClient(reply="", status="failed", error={"code": "server_error", "message": "boom"}), call)
    assert failed["error_kind"] == "failed" and failed["response_error"]["message"] == "boom"
    truncated = call_monitor(FakeClient(reply="", status="incomplete", incomplete={"reason": "max_output_tokens"}), call)
    assert truncated["error"] is None and truncated["truncated"] and truncated["parse_failed"]
    assert truncated["incomplete_details"] == {"reason": "max_output_tokens"} and truncated["output_items"]


def test_parse_failures_are_null_and_retried_only_on_request_unless_truncated(tmp_path):
    out = tmp_path / "r.jsonl"
    _, calls = _calls()
    counts = run(FakeClient(reply="no score here"), calls, out, workers=1)
    assert counts["parse_failures"] == 1 and '"score": null' in out.read_text()
    assert run(FakeClient(), calls, out, workers=1)["calls"] == 0
    assert run(FakeClient(), calls, out, workers=1, retry_parse_failures=True)["calls"] == 1

    out2 = tmp_path / "t.jsonl"
    run(FakeClient(reply="", status="incomplete"), calls, out2, workers=1)
    assert run(FakeClient(), calls, out2, workers=1, retry_parse_failures=True)["calls"] == 0


def test_truncated_last_line_is_skipped_and_not_glued_to_the_next_record(tmp_path):
    out = tmp_path / "r.jsonl"
    _, one = _calls(readings=1)
    _, two = _calls(readings=2)
    run(FakeClient(), one, out, workers=1)
    with out.open("a") as f:
        f.write('{"key": "cut off')
    assert len(done_keys(out)) == 1
    run(FakeClient(), two, out, workers=1)
    assert len(done_keys(out)) == 2


def test_load_results_keeps_one_current_record_per_reading_and_shares_across_jobs(tmp_path):
    out = tmp_path / "r.jsonl"
    jobs = build_jobs(SCENARIOS, ["per_agent"])
    calls = plan_calls(jobs, [MODEL], 1, 1.0, 100)
    run(FakeClient(fail=ConnectionError("down")), calls, out, workers=2)
    run(FakeClient(), calls, out, workers=2)
    with out.open("a") as f:
        f.write(json.dumps({"key": "stale|old prompt", "score": 1, "error": None}) + "\n")
    rows = load_results(out, calls, jobs)
    assert len(rows) == len(jobs)
    assert all(not r["missing"] and r["error"] is None and r["score"] == 72 for r in rows)


def test_credit_cap_stops_new_calls(tmp_path):
    out = tmp_path / "r.jsonl"
    _, calls = _calls(SCENARIOS[:4], readings=5)
    client = FakeClient(cost=0.01)
    counts = run(client, calls, out, workers=1, max_credits=0.03)
    assert counts["stopped"] == "max_credits"
    assert 3 <= len(client.calls) < len(calls) and counts["written"] == len(client.calls)


def test_real_ctrl_c_cancels_queued_calls_and_saves_paid_ones(tmp_path):
    out = tmp_path / "r.jsonl"
    _, calls = _calls(SCENARIOS, ("solo", "team_grouped"), readings=5)
    client = FakeClient(delay=0.05)

    def interrupt_after_a_few_calls():
        while len(client.calls) < 3:
            time.sleep(0.005)
        os.kill(os.getpid(), signal.SIGINT)

    threading.Thread(target=interrupt_after_a_few_calls, daemon=True).start()
    counts = run(client, calls, out, workers=2)
    assert counts["stopped"] == "interrupt"
    records = _records(out)
    assert len(client.calls) < len(calls)
    assert len(records) == len(client.calls) == counts["written"]
    assert len({r["key"] for r in records}) == len(records)
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


def test_a_second_run_on_the_same_file_is_refused(tmp_path):
    out = tmp_path / "r.jsonl"
    _, calls = _calls()
    from monitor import _open_for_append

    holder = _open_for_append(out)
    try:
        with pytest.raises(RuntimeError):
            run(FakeClient(), calls, out, workers=1)
    finally:
        holder.close()


def test_simple_namespace_responses_still_work():
    """Responses without pydantic extras, as some SDK versions may return."""
    _, calls = _calls()

    class Plain(FakeClient):
        def create(self, **kwargs):
            usage = SimpleNamespace(input_tokens=1, output_tokens=1, output_tokens_details=None)
            return SimpleNamespace(output_text=REPLY, usage=usage, model=MODEL, status="completed", incomplete_details=None)

    record = call_monitor(Plain(), next(iter(calls.values())))
    assert record["score"] == 72 and record["cost"] > 0
