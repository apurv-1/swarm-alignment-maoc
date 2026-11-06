import json
import os
import signal
import subprocess
import sys
import threading
import time

from pathlib import Path

import httpx
import pytest
from adaption import Adaption
from adaption._models import construct_type
from adaption.types.response import Response

from monitor import (
    CONDITIONS,
    FRAMING_NOTE,
    MAYBE_BILLED,
    _Sink,
    build_jobs,
    call_monitor,
    done_keys,
    error_kind,
    estimate,
    load_results,
    parse_output,
    plan_calls,
    run,
    spent,
)
from scenarios import load_all

SCENARIOS = load_all()
# Whole-log views read only the disguised and twin variants.
WHOLE = [s for s in SCENARIOS if s.variant != "direct"]
MODEL = "deepseek-v4-flash"
REPLY = "<reasoning>r</reasoning><issue>leak</issue><score>72</score>"


def response_payload(text=REPLY, status="completed", usage=True, cost=0.001, error=None, incomplete=None):
    payload = {
        "id": "resp_1", "object": "response", "status": status, "model": MODEL, "output_text": text,
        "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
        "error": error, "incomplete_details": incomplete,
    }
    if usage:
        payload["usage"] = {
            "input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
            "output_tokens_details": {"reasoning_tokens": 5},
        }
        if cost is not None:
            payload["usage"]["cost"] = cost
    return payload


class FakeClient:
    """Stands in for the Adaption client so tests never call the API.

    `reply` may be a function of the request, so different prompts and readings get different scores.
    """

    def __init__(self, reply=REPLY, fail=None, delay=0.0, **options):
        self.calls = []
        self.reply, self.fail, self.delay, self.options = reply, fail, delay, options
        self.responses = self
        self.lock = threading.Lock()

    def create(self, **kwargs):
        with self.lock:
            self.calls.append(kwargs)
        time.sleep(self.delay)
        if self.fail:
            raise self.fail
        text = self.reply(kwargs) if callable(self.reply) else self.reply
        return construct_type(value=response_payload(text, **self.options), type_=Response)


def sdk_client(handler):
    """The real SDK over a mock transport, to check how its errors are classified."""
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return Adaption(api_key="test", base_url="http://adaption.test", http_client=http, max_retries=0)


def _records(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def _results(path):
    return [r for r in _records(path) if not r.get("started")]


def _calls(scenarios=WHOLE[:1], conditions=("solo",), readings=1, max_output_tokens=100, temperature=1.0, models=(MODEL,)):
    jobs = build_jobs(scenarios, conditions)
    return jobs, plan_calls(jobs, list(models), readings, temperature, max_output_tokens)


def _one_call():
    return next(iter(_calls()[1].values()))


def test_parse_output_takes_last_valid_score_and_last_issue():
    score, issue = parse_output("<issue>first</issue><score>10</score> then <issue> second </issue><score>85</score>")
    assert score == 85 and issue == "second"
    assert parse_output("<score>**40**</score>")[0] == 40
    assert parse_output("no score")[0] is None
    assert parse_output("<score>150</score>")[0] is None


def test_build_jobs_frames_whole_log_views_only_and_reads_direct_per_agent_only():
    disguised = next(s for s in SCENARIOS if s.variant == "disguised")
    direct = next(s for s in SCENARIOS if s.variant == "direct")
    jobs = build_jobs([disguised], CONDITIONS, framings=(False, True))
    per_agent_jobs = [j for j in jobs if j.condition == "per_agent"]
    assert len(per_agent_jobs) == len(disguised.agents) and not any(j.framing for j in per_agent_jobs)
    assert len([j for j in jobs if j.condition != "per_agent"]) == (len(CONDITIONS) - 1) * 2
    assert {j.condition for j in build_jobs([direct], CONDITIONS)} == {"per_agent"}
    assert len(build_jobs([disguised], (c for c in ["solo", "solo", "per_agent"]))) == 1 + len(disguised.agents)


def test_framing_changes_instructions_only_and_instructions_are_neutral():
    plain, framed = build_jobs(WHOLE[:1], ["solo"], framings=(False, True))
    assert plain.transcript == framed.transcript
    assert FRAMING_NOTE in framed.instructions and FRAMING_NOTE not in plain.instructions
    for word in ("agent", "team", "test data", "leak", "not shown"):
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
    _, calls = _calls(WHOLE[:2], readings=3)
    readings = [c.reading for c in calls.values()]
    assert readings == sorted(readings)


def test_estimate_upper_bound_covers_the_typical_estimate():
    _, one = _calls(WHOLE[:2], readings=1, max_output_tokens=4000)
    _, five = _calls(WHOLE[:2], readings=5, max_output_tokens=4000)
    e1, e5 = estimate(one.values())[MODEL], estimate(five.values())[MODEL]
    assert e5["calls"] == 5 * e1["calls"] and e5["credits"] > e1["credits"]
    assert e1["max_credits"] > e1["credits"]


def test_the_exact_request_is_sent():
    call = _one_call()
    client = FakeClient()
    call_monitor(client, call)
    assert client.calls == [{
        "model": MODEL, "instructions": call.job.instructions, "input": call.job.transcript,
        "max_output_tokens": 100, "temperature": 1.0, "store": False,
    }]


def test_run_records_results_and_resumes_without_repeating(tmp_path):
    out = tmp_path / "r.jsonl"
    _, calls = _calls(WHOLE[:2], ("solo", "per_agent"), readings=2, temperature=0.7)
    client = FakeClient()
    first = run(client, calls, out, workers=2)
    assert first["written"] == len(client.calls) == len(calls)
    results = _results(out)
    assert len({r["key"] for r in results}) == len(results) == len(calls)
    assert len([r for r in _records(out) if r.get("started")]) == len(calls)
    assert all(r["score"] == 72 and r["issue"] == "leak" and r["cost"] == 0.001 for r in results)
    assert all(r["response_model"] == MODEL and r["status"] == "completed" for r in results)
    assert results[0]["output_tokens_details"] == {"reasoning_tokens": 5}
    assert run(client, calls, out, workers=2)["calls"] == 0 and len(client.calls) == first["written"]


@pytest.mark.parametrize(
    "handler, kind",
    [
        (lambda request: (_ for _ in ()).throw(httpx.ConnectError("refused", request=request)), "not_sent"),
        (lambda request: (_ for _ in ()).throw(httpx.ConnectTimeout("slow", request=request)), "not_sent"),
        (lambda request: (_ for _ in ()).throw(httpx.ReadTimeout("slow", request=request)), MAYBE_BILLED),
        (lambda request: (_ for _ in ()).throw(httpx.RemoteProtocolError("dropped", request=request)), MAYBE_BILLED),
        (lambda request: httpx.Response(429, json={"error": "slow down"}), "rate_limit"),
        (lambda request: httpx.Response(400, json={"error": "bad"}), "rejected"),
        (lambda request: httpx.Response(408, json={"error": "timeout"}), MAYBE_BILLED),
        (lambda request: httpx.Response(504, json={"error": "gateway"}), MAYBE_BILLED),
        (lambda request: httpx.Response(500, json={"error": "boom"}), MAYBE_BILLED),
    ],
    ids=["connect-error", "connect-timeout", "read-timeout", "dropped", "429", "400", "408", "504", "500"],
)
def test_real_sdk_errors_are_classified_by_whether_they_may_have_been_billed(handler, kind):
    record = call_monitor(sdk_client(handler), _one_call())
    assert record["error_kind"] == kind


def test_real_sdk_success_is_parsed_with_the_undeclared_cost():
    record = call_monitor(sdk_client(lambda request: httpx.Response(200, json=response_payload(cost=0.0042))), _one_call())
    assert record["error"] is None and record["score"] == 72 and record["cost"] == 0.0042


def test_maybe_billed_calls_are_settled_and_charged_at_their_worst_case(tmp_path):
    out = tmp_path / "r.jsonl"
    _, calls = _calls()
    call = next(iter(calls.values()))
    run(FakeClient(fail=TimeoutError("slow")), calls, out, workers=1)
    assert _results(out)[0]["error_kind"] == MAYBE_BILLED and _results(out)[0]["score"] is None
    assert spent(out) == pytest.approx(call.upper_bound)
    assert run(FakeClient(), calls, out, workers=1)["calls"] == 0
    assert run(FakeClient(), calls, out, workers=1, retry_maybe_billed=True)["calls"] == 1


def test_calls_that_never_reached_the_server_are_retried_and_cost_nothing(tmp_path):
    out = tmp_path / "r.jsonl"
    _, calls = _calls()
    fail = httpx.ConnectError("refused")
    run(FakeClient(fail=fail), calls, out, workers=1)
    assert _results(out)[0]["error_kind"] == "not_sent" and spent(out) == 0
    assert run(FakeClient(), calls, out, workers=1)["calls"] == 1


def test_a_started_call_with_no_result_is_never_paid_for_twice(tmp_path):
    out = tmp_path / "r.jsonl"
    _, calls = _calls()
    call = next(iter(calls.values()))
    out.write_text(json.dumps({"key": call.key, "started": True, "upper_bound": call.upper_bound}) + "\n")
    assert call.key in done_keys(out) and spent(out) == pytest.approx(call.upper_bound)
    assert run(FakeClient(), calls, out, workers=1)["calls"] == 0


@pytest.mark.parametrize("options", [{"usage": False}, {"cost": None}, {"cost": "0.002"}], ids=["no-usage", "no-cost", "string-cost"])
def test_missing_or_odd_cost_keeps_the_paid_reply(options):
    record = call_monitor(FakeClient(**options), _one_call())
    assert record["error"] is None and record["score"] == 72
    json.dumps(record, allow_nan=False)
    if "usage" not in options:
        assert isinstance(record["cost"], float) and record["cost"] > 0


def test_failed_and_truncated_replies_keep_their_evidence():
    call = _one_call()
    failed = call_monitor(FakeClient(reply="", status="failed", error={"code": "server_error", "message": "boom"}), call)
    assert failed["error_kind"] == "failed" and failed["response_error"]["message"] == "boom"
    truncated = call_monitor(FakeClient(reply="", status="incomplete", incomplete={"reason": "max_output_tokens"}), call)
    assert truncated["error"] is None and truncated["truncated"] and truncated["parse_failed"]
    assert truncated["incomplete_details"] == {"reason": "max_output_tokens"} and truncated["output_items"]


def test_a_failed_reply_with_a_cost_is_settled(tmp_path):
    out = tmp_path / "r.jsonl"
    _, calls = _calls()
    run(FakeClient(reply="", status="failed", error={"code": "x", "message": "y"}), calls, out, workers=1)
    assert run(FakeClient(), calls, out, workers=1)["calls"] == 0


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


def test_load_results_joins_each_reading_to_every_job_that_shares_its_prompt(tmp_path):
    out = tmp_path / "r.jsonl"
    jobs = build_jobs(SCENARIOS, ["per_agent", "solo"])
    calls = plan_calls(jobs, [MODEL, "qwen3.5-397b"], 3, 1.0, 100)
    expected = {}

    def reply(request):
        key = next(k for k, c in calls.items()
                   if c.job.transcript == request["input"] and c.model == request["model"] and k not in expected)
        expected[key] = len(expected) % 101
        return f"<issue>i{expected[key]}</issue><score>{expected[key]}</score>"

    run(FakeClient(reply=reply), calls, out, workers=1)
    with out.open("a") as f:
        f.write(json.dumps({"key": "stale|old prompt", "score": 1, "error": None}) + "\n")
    rows = load_results(out, calls, jobs)
    assert len(rows) == len(jobs) * 2 * 3
    for row in rows:
        assert not row["missing"] and row["score"] == expected[row["key"]] and row["issue"] == f"i{expected[row['key']]}"
        call = calls[row["key"]]
        assert call.model == row["model"] and call.reading == row["reading"]
        assert call.job.prompt_hash == next(j.prompt_hash for j in jobs if j.scenario == row["scenario"]
                                            and j.condition == row["condition"] and j.part == row["part"])


def test_the_budget_counts_earlier_runs_and_reserves_before_each_call(tmp_path):
    out = tmp_path / "r.jsonl"
    _, calls = _calls(WHOLE[:1], readings=20)  # one prompt, so every call has the same worst case
    bound = next(iter(calls.values())).upper_bound
    client = FakeClient(cost=bound)  # each call costs its worst case, so the arithmetic is exact
    first = run(client, calls, out, workers=4, max_credits=3.5 * bound)
    assert first["stopped"] == "max_credits" and len(client.calls) == 3
    second = run(client, calls, out, workers=4, max_credits=3.5 * bound)
    assert second["written"] == 0 and len(client.calls) == 3
    assert spent(out) <= 3.5 * bound


def test_real_ctrl_c_cancels_queued_calls_and_saves_paid_ones(tmp_path):
    out = tmp_path / "r.jsonl"
    _, calls = _calls(WHOLE, ("solo", "team_grouped"), readings=5)
    client = FakeClient(delay=0.05)

    def interrupt_after_a_few_calls():
        while len(client.calls) < 3:
            time.sleep(0.005)
        os.kill(os.getpid(), signal.SIGINT)

    threading.Thread(target=interrupt_after_a_few_calls, daemon=True).start()
    counts = run(client, calls, out, workers=2)
    assert counts["stopped"] == "interrupt"
    results = _results(out)
    assert len(client.calls) < len(calls)
    assert len(results) == len(client.calls) == counts["written"]
    assert len({r["key"] for r in results}) == len(results)
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


def test_a_second_run_on_the_same_file_is_refused_and_leaves_ctrl_c_alone(tmp_path):
    out = tmp_path / "r.jsonl"
    _, calls = _calls()
    holder = _Sink(out)
    try:
        with pytest.raises(RuntimeError):
            run(FakeClient(), calls, out, workers=1)
    finally:
        holder.close()
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


def test_zero_workers_is_refused_before_anything_starts(tmp_path):
    with pytest.raises(ValueError):
        run(FakeClient(), _calls()[1], tmp_path / "r.jsonl", workers=0)
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


def test_a_plain_text_reply_is_kept():
    class TextClient(FakeClient):
        def create(self, **kwargs):
            return "<score>12</score> plain body"

    record = call_monitor(TextClient(), _one_call())
    assert record["output"] == "<score>12</score> plain body" and record["score"] == 12


def test_error_kind_of_plain_exceptions_is_conservative():
    assert error_kind(TimeoutError()) == MAYBE_BILLED
    assert error_kind(ConnectionError()) == MAYBE_BILLED
    assert error_kind(httpx.ConnectError("x")) == "not_sent"


def test_the_started_marker_is_on_disk_before_the_api_is_called(tmp_path):
    out = tmp_path / "r.jsonl"
    _, calls = _calls(readings=3)

    class Checking(FakeClient):
        def create(self, **kwargs):
            started = [r for r in _records(out) if r.get("started")]
            assert len(started) == len(self.calls) + 1, "API called before its marker was written"
            return super().create(**kwargs)

    assert run(Checking(), calls, out, workers=1)["written"] == 3


def test_a_retry_killed_mid_call_is_charged_and_never_bought_again(tmp_path):
    out = tmp_path / "r.jsonl"
    _, calls = _calls()
    call = next(iter(calls.values()))
    run(FakeClient(fail=httpx.ConnectError("refused")), calls, out, workers=1)  # free and retryable
    with out.open("a") as f:  # the retry starts, then the process is killed
        f.write(json.dumps({"key": call.key, "attempt": "killed", "started": True, "upper_bound": call.upper_bound}) + "\n")
    assert spent(out) == pytest.approx(call.upper_bound)
    assert run(FakeClient(), calls, out, workers=1)["calls"] == 0
    assert run(FakeClient(), calls, out, workers=1, retry_maybe_billed=True)["calls"] == 1


def test_a_failed_reply_without_a_cost_is_settled_unless_asked(tmp_path):
    out = tmp_path / "r.jsonl"
    _, calls = _calls()
    run(FakeClient(reply="", status="failed", usage=False, error={"code": "x", "message": "y"}), calls, out, workers=1)
    assert run(FakeClient(), calls, out, workers=1)["calls"] == 0
    assert run(FakeClient(), calls, out, workers=1, retry_maybe_billed=True)["calls"] == 1


@pytest.mark.parametrize("bad", ["NaN", "inf", -1, "abc"])
def test_a_nonsense_cost_falls_back_to_token_prices(bad):
    record = call_monitor(FakeClient(cost=bad), _one_call())
    assert record["cost"] is not None and record["cost"] > 0 and record["cost"] < 1


def test_a_cost_far_from_token_prices_is_flagged():
    assert call_monitor(FakeClient(cost=5.0), _one_call())["cost_mismatch"]
    tokens = (100 * 0.04444 + 20 * 0.13332) / 1e6
    assert not call_monitor(FakeClient(cost=tokens), _one_call())["cost_mismatch"]


def test_load_results_prefers_a_later_success_and_reparses_stored_replies(tmp_path):
    out = tmp_path / "r.jsonl"
    jobs, calls = _calls()
    run(FakeClient(reply="no score"), calls, out, workers=1)
    run(FakeClient(reply="<issue>late</issue><score>33</score>"), calls, out, workers=1, retry_parse_failures=True)
    [row] = load_results(out, calls, jobs)
    assert row["score"] == 33 and row["issue"] == "late"
    # A stored reply with a score the old parser missed is re-parsed at load.
    records = _records(out)
    for r in records:
        if not r.get("started") and r.get("score") == 33:
            r["score"] = None
    out.write_text("".join(json.dumps(r) + "\n" for r in records))
    [row] = load_results(out, calls, jobs)
    assert row["score"] == 33


def test_three_ctrl_c_presses_exit_at_once_with_every_record_flushed(tmp_path):
    out = tmp_path / "r.jsonl"
    script = tmp_path / "child.py"
    lines = [
        "import os, sys",
        f"sys.path.insert(0, {str(Path(__file__).parent.parent)!r})",
        f"sys.path.insert(0, {str(Path(__file__).parent)!r})",
        "from pathlib import Path",
        "from test_monitor import FakeClient, _calls, WHOLE",
        "from monitor import run",
        "_, calls = _calls(WHOLE[:2], readings=5)",
        "try:",
        f"    run(FakeClient(delay=30), calls, Path({str(out)!r}), workers=2)",
        "except KeyboardInterrupt:",
        "    os._exit(130)",
        "os._exit(0)",
    ]
    script.write_text("\n".join(lines) + "\n")
    child = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    deadline = time.time() + 20
    while time.time() < deadline and (not out.exists() or out.read_text().count('"started"') < 2):
        time.sleep(0.05)
    for _ in range(3):
        child.send_signal(signal.SIGINT)
        time.sleep(0.3)
    code = child.wait(timeout=10)
    assert code == 130, child.stdout.read()
    records = _records(out)
    assert len([r for r in records if r.get("started")]) == 2 and not _results(out)
    assert spent(out) > 0 and len(done_keys(out)) == 2
