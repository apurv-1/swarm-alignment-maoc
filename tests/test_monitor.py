import json
import math
from types import SimpleNamespace

from monitor import CONDITIONS, build_jobs, done_keys, estimate, instructions_for, parse_output, run
from scenarios import load_all

SCENARIOS = load_all()


class FakeClient:
    """Stands in for the Adaption client so tests never call the API."""

    def __init__(self, reply="<reasoning>r</reasoning><issue>leak</issue><score>72</score>", fail=False):
        self.calls = 0
        self.reply = reply
        self.fail = fail
        self.responses = self

    def create(self, **kwargs):
        self.calls += 1
        if self.fail:
            raise TimeoutError("slow")
        usage = SimpleNamespace(input_tokens=100, output_tokens=20, cost=0.001)
        return SimpleNamespace(output_text=self.reply, usage=usage)


def test_parse_output_takes_last_valid_score_and_issue():
    score, issue = parse_output("<score>10</score> then <issue> test leak </issue> <score>85</score>")
    assert score == 85 and issue == "test leak"
    assert math.isnan(parse_output("no score")[0])
    assert math.isnan(parse_output("<score>150</score>")[0])


def test_build_jobs_counts_per_agent_parts_and_skips_state_for_per_agent():
    scenario = SCENARIOS[0]
    jobs = build_jobs([scenario], CONDITIONS, framings=(False,), final_states=(False, True))
    per_agent_jobs = [j for j in jobs if j.condition == "per_agent"]
    assert len(per_agent_jobs) == len(scenario.agents)
    assert not any(j.final_state for j in per_agent_jobs)
    whole_team = [j for j in jobs if j.condition != "per_agent"]
    assert len(whole_team) == (len(CONDITIONS) - 1) * 2
    assert all("Final state of the project" in j.transcript for j in whole_team if j.final_state)


def test_framing_changes_instructions_only():
    plain, framed = build_jobs(SCENARIOS[:1], ["solo"], framings=(False, True))
    assert plain.transcript == framed.transcript
    assert instructions_for(framed) != instructions_for(plain)


def test_estimate_scales_with_readings():
    jobs = build_jobs(SCENARIOS[:2], ["solo"])
    one = estimate(jobs, ["deepseek-v4-flash"], readings=1)["deepseek-v4-flash"]
    five = estimate(jobs, ["deepseek-v4-flash"], readings=5)["deepseek-v4-flash"]
    assert five["calls"] == 5 * one["calls"]
    assert five["credits"] > one["credits"]


def test_run_records_results_and_resumes_without_repeating(tmp_path):
    out = tmp_path / "r.jsonl"
    jobs = build_jobs(SCENARIOS[:2], ["solo", "per_agent"])
    client = FakeClient()
    first = run(client, jobs, ["deepseek-v4-flash"], 2, out, workers=2, max_output_tokens=100)
    assert first == client.calls == len(jobs) * 2
    records = [json.loads(line) for line in out.read_text().splitlines()]
    assert all(r["score"] == 72 and r["issue"] == "leak" and "transcript" not in r for r in records)

    second = run(client, jobs, ["deepseek-v4-flash"], 2, out, workers=2, max_output_tokens=100)
    assert second == 0 and client.calls == first


def test_failed_calls_are_recorded_and_retried_later(tmp_path):
    out = tmp_path / "r.jsonl"
    jobs = build_jobs(SCENARIOS[:1], ["solo"])
    run(FakeClient(fail=True), jobs, ["deepseek-v4-flash"], 1, out, workers=1, max_output_tokens=100)
    record = json.loads(out.read_text())
    assert record["error"].startswith("TimeoutError") and record["parse_failed"]
    assert done_keys(out) == set()
    retry = FakeClient()
    assert run(retry, jobs, ["deepseek-v4-flash"], 1, out, workers=1, max_output_tokens=100) == 1
