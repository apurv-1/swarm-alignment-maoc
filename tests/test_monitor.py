import json
from types import SimpleNamespace

import pytest

from monitor import CONDITIONS, FRAMING_NOTE, build_jobs, call_monitor, done_keys, estimate, parse_output, run
from scenarios import load_all

SCENARIOS = load_all()
MODEL = "deepseek-v4-flash"
REPLY = "<reasoning>r</reasoning><issue>leak</issue><score>72</score>"


class FakeClient:
    """Stands in for the Adaption client so tests never call the API."""

    def __init__(self, reply=REPLY, fail=False, usage=True, cost=True):
        self.calls = []
        self.reply = reply
        self.fail = fail
        self.usage = usage
        self.cost = cost
        self.responses = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise TimeoutError("slow")
        usage = None
        if self.usage:
            usage = SimpleNamespace(input_tokens=100, output_tokens=20, output_tokens_details=None)
            if self.cost:
                usage.cost = 0.001
        return SimpleNamespace(output_text=self.reply, usage=usage, model=kwargs["model"], status="completed", incomplete_details=None)


def _records(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_parse_output_takes_last_valid_score_and_issue():
    score, issue = parse_output("<score>10</score> then <issue> test leak </issue> <score>85</score>")
    assert score == 85 and issue == "test leak"
    assert parse_output("no score")[0] is None
    assert parse_output("<score>150</score>")[0] is None


def test_build_jobs_counts_parts_and_frames_whole_log_views_only():
    scenario = SCENARIOS[0]
    jobs = build_jobs([scenario], CONDITIONS, framings=(False, True))
    per_agent_jobs = [j for j in jobs if j.condition == "per_agent"]
    assert len(per_agent_jobs) == len(scenario.agents)
    assert not any(j.framing for j in per_agent_jobs)
    whole_log = [j for j in jobs if j.condition != "per_agent"]
    assert len(whole_log) == (len(CONDITIONS) - 1) * 2


def test_framing_changes_instructions_only_and_instructions_are_neutral():
    plain, framed = build_jobs(SCENARIOS[:1], ["solo"], framings=(False, True))
    assert plain.transcript == framed.transcript
    assert FRAMING_NOTE in framed.instructions and FRAMING_NOTE not in plain.instructions
    for word in ("agent", "team", "test data", "leak"):
        assert word not in plain.instructions.lower()
    # The note comes before the answer format, not after it.
    assert framed.instructions.index(FRAMING_NOTE) < framed.instructions.index("Answer in this format")


def test_prompt_hash_tracks_the_exact_prompt():
    plain, framed = build_jobs(SCENARIOS[:1], ["solo"], framings=(False, True))
    assert plain.prompt_hash != framed.prompt_hash
    assert plain.key(MODEL, 1.0, 0) != plain.key(MODEL, 0.7, 0)


def test_estimate_scales_with_readings_and_has_an_upper_bound():
    jobs = build_jobs(SCENARIOS[:2], ["solo"])
    one = estimate(jobs, [MODEL], readings=1, max_output_tokens=4000)[MODEL]
    five = estimate(jobs, [MODEL], readings=5, max_output_tokens=4000)[MODEL]
    assert five["calls"] == 5 * one["calls"]
    assert five["credits"] > one["credits"]
    assert one["max_credits"] > one["credits"]


def test_run_records_results_and_resumes_without_repeating(tmp_path):
    out = tmp_path / "r.jsonl"
    jobs = build_jobs(SCENARIOS[:2], ["solo", "per_agent"])
    client = FakeClient()
    first = run(client, jobs, [MODEL], 2, out, workers=2, max_output_tokens=100, temperature=0.7)
    assert first["written"] == len(client.calls) == len(jobs) * 2
    assert all(call["temperature"] == 0.7 for call in client.calls)
    records = _records(out)
    assert all(r["score"] == 72 and r["issue"] == "leak" and "transcript" not in r for r in records)
    assert all(r["cost"] == 0.001 and r["response_model"] == MODEL and r["temperature"] == 0.7 for r in records)

    second = run(client, jobs, [MODEL], 2, out, workers=2, max_output_tokens=100, temperature=0.7)
    assert second["calls"] == 0 and len(client.calls) == first["written"]


def test_failed_calls_are_recorded_and_retried_later(tmp_path):
    out = tmp_path / "r.jsonl"
    jobs = build_jobs(SCENARIOS[:1], ["solo"])
    counts = run(FakeClient(fail=True), jobs, [MODEL], 1, out, workers=1, max_output_tokens=100)
    record = _records(out)[0]
    assert record["error"].startswith("TimeoutError") and record["parse_failed"] and record["score"] is None
    assert counts["errors"] == 1 and done_keys(out) == set()
    assert run(FakeClient(), jobs, [MODEL], 1, out, workers=1, max_output_tokens=100)["calls"] == 1


@pytest.mark.parametrize("client", [FakeClient(usage=False), FakeClient(cost=False)], ids=["no-usage", "no-cost"])
def test_missing_usage_or_cost_keeps_the_paid_reply(client):
    job = build_jobs(SCENARIOS[:1], ["solo"])[0]
    record = call_monitor(client, MODEL, job, 0, 100, 1.0)
    assert record["error"] is None and record["score"] == 72 and record["cost"] is None
    json.dumps(record, allow_nan=False)


def test_parse_failures_are_written_as_null_and_retried_only_on_request(tmp_path):
    out = tmp_path / "r.jsonl"
    jobs = build_jobs(SCENARIOS[:1], ["solo"])
    counts = run(FakeClient(reply="no score here"), jobs, [MODEL], 1, out, workers=1, max_output_tokens=100)
    assert counts["parse_failures"] == 1
    assert '"score": null' in out.read_text()
    assert run(FakeClient(), jobs, [MODEL], 1, out, workers=1, max_output_tokens=100)["calls"] == 0
    retried = run(FakeClient(), jobs, [MODEL], 1, out, workers=1, max_output_tokens=100, retry_parse_failures=True)
    assert retried["calls"] == 1


def test_truncated_last_line_is_skipped_and_not_glued_to_the_next_record(tmp_path):
    out = tmp_path / "r.jsonl"
    jobs = build_jobs(SCENARIOS[:1], ["solo"])
    run(FakeClient(), jobs, [MODEL], 1, out, workers=1, max_output_tokens=100)
    with out.open("a") as f:
        f.write('{"key": "cut off')
    assert len(done_keys(out)) == 1
    run(FakeClient(), jobs, [MODEL], 2, out, workers=1, max_output_tokens=100)
    assert len(done_keys(out)) == 2


def test_interrupt_cancels_queued_calls(tmp_path):
    class InterruptingClient(FakeClient):
        def create(self, **kwargs):
            if len(self.calls) >= 2:
                raise KeyboardInterrupt
            return super().create(**kwargs)

    out = tmp_path / "r.jsonl"
    jobs = build_jobs(SCENARIOS[:4], ["solo"])
    client = InterruptingClient()
    with pytest.raises(KeyboardInterrupt):
        run(client, jobs, [MODEL], 5, out, workers=1, max_output_tokens=100)
    assert len(client.calls) < len(jobs) * 5
    assert all(r["error"] is None for r in _records(out))
