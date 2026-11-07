import json

import pytest

import check_monitor
from main_report import INCLUDED_FAMILIES, WHOLE_VIEWS, grouped_order_views, timing_scenarios
from monitor import build_jobs


def test_export_writes_every_whole_log_prompt_without_naming_the_variant(tmp_path):
    out = tmp_path / "p.jsonl"
    count = check_monitor.export(out)
    lines = [json.loads(line) for line in out.read_text().splitlines()]
    assert count == len(lines) == len(INCLUDED_FAMILIES) * 2 * len(WHOLE_VIEWS)
    assert set(lines[0]) == {"id", "family", "instructions", "transcript"}
    assert len({line["id"] for line in lines}) == count
    assert not any(word in line["id"] for line in lines for word in ("twin", "disguised", "grouped"))


def test_score_reproduces_a_layout_cost_from_outside_scores(tmp_path):
    prompts = tmp_path / "p.jsonl"
    check_monitor.export(prompts)
    scenarios = timing_scenarios(list(INCLUDED_FAMILIES))
    lines = []
    for job in build_jobs(scenarios, WHOLE_VIEWS):
        misleading = grouped_order_views(scenarios, job.family)["attack_order"] == job.condition
        score = 100 if job.is_attack or misleading else 0  # a monitor fooled only by attack-order blocks
        lines.append(json.dumps({"id": job.prompt_hash, "score": score}))
    scores = tmp_path / "s.jsonl"
    scores.write_text("\n".join(lines) + "\n")
    text = "\n".join(check_monitor.report(*check_monitor.score_rows(prompts, scores)))
    assert "mean +50.0 over 9 families, 9 positive" in text and "p = 0.0020" in text
    assert "  grouped, attack's order          100%     100%" in text


def test_score_rejects_unknown_ids_and_scores_out_of_range(tmp_path):
    prompts = tmp_path / "p.jsonl"
    check_monitor.export(prompts)
    known = json.loads(prompts.read_text().splitlines()[0])["id"]
    scores = tmp_path / "s.jsonl"
    scores.write_text(json.dumps({"id": "nope", "score": 1}) + "\n")
    with pytest.raises(ValueError, match="unknown id"):
        check_monitor.score_rows(prompts, scores)
    scores.write_text(json.dumps({"id": known, "score": 150}) + "\n")
    with pytest.raises(ValueError, match="outside 0 to 100"):
        check_monitor.score_rows(prompts, scores)
