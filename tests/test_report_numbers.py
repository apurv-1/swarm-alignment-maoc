from statistics import mean

import report_numbers
from main_report import GROUPED, INTERLEAVED, SOLO, SWAPPED, grouped_order_views, timing_scenarios
from report_numbers import (ATTACK_ORDER, TIME_ORDER, TWIN_ORDER, Arm, cutoff_section, family_view_rates, num, pct,
                            sign_flip_summary, threshold_at_budget, unmatched_numbers)


def _row(family, condition, variant, score, truncated=False):
    return {"family": family, "condition": condition, "variant": variant, "score": score,
            "error": None, "truncated": truncated}


def test_family_view_rates_names_grouped_views_by_their_order():
    family = "race-4"
    views = grouped_order_views(timing_scenarios([family]), family)
    rows = [
        _row(family, views["attack_order"], "disguised", 100), _row(family, views["attack_order"], "twin", 100),
        _row(family, views["twin_order"], "disguised", 0), _row(family, views["twin_order"], "twin", 0),
        _row(family, INTERLEAVED, "disguised", 100), _row(family, INTERLEAVED, "twin", 0),
        _row(family, SOLO, "disguised", 100), _row(family, SOLO, "twin", 0),
    ]
    rates = family_view_rates(Arm("m", False, [family], rows))[family]
    assert rates[ATTACK_ORDER] == (1.0, 1.0)
    assert rates[TWIN_ORDER] == (0.0, 0.0)
    assert rates[TIME_ORDER] == (1.0, 0.0)
    assert rates[SOLO] == (1.0, 0.0)


def test_family_view_rates_leave_out_missing_readings():
    family = "race-4"
    rows = [_row(family, INTERLEAVED, "disguised", 100), _row(family, INTERLEAVED, "disguised", 0, truncated=True)]
    rates = family_view_rates(Arm("m", False, [family], rows))[family]
    assert rates[TIME_ORDER] == (1.0, None)


def test_sign_flip_summary_reports_every_family_and_without_race_11():
    summary = sign_flip_summary({"race-1": 10.0, "race-11": 50.0, "race-2": -2.0, "race-3": None})
    assert summary["n"] == 3
    assert summary["positive"] == 2
    assert summary["n_without"] == 2
    assert summary["mean_without"] == 4.0


def test_sign_flip_summary_matches_long_family_names():
    summary = sign_flip_summary({"long-race-11": 5.0, "long-race-1": 1.0})
    assert summary["n_without"] == 1


def test_formatting_shows_missing_values_as_na():
    assert pct(None) == "n/a"
    assert pct(0.284) == "28%"
    assert num(None) == "n/a"
    assert num(21.04) == "+21.0"


REPORT = """# Title
Intro says 31.7% of attacks were flagged.
## Abstract
Grouping cut separation by 21.0 points (p = 0.002), and 28% of twins were flagged.
## 1. Introduction
Kitaoka reports 81% at 8.5.
## 4. Results
### 4.1 Heading numbers are ignored
glm-5.2's delta was +1.8, as Section 3.4 predicts.
## 5. Discussion
Nothing here is checked: 99.9.
"""


def test_unmatched_numbers_flags_stale_results_and_accepts_rounding():
    numbers = "| deepseek | +21.0 | 0.0020 | 28% |\n| glm-5.2 | +1.5 |"
    assert unmatched_numbers(REPORT, numbers) == ["1.8"]


def test_unmatched_numbers_passes_when_every_number_is_generated():
    assert unmatched_numbers(REPORT, "+21.0 0.0020 28% +1.8") == []


def test_cutoff_section_counts_cut_off_replies_per_view_and_compares_their_cost(monkeypatch):
    rows = [{**_row("race-4", view, variant, None if (view, variant) == (SOLO, "twin") else 0,
                    truncated=(view, variant) == (SOLO, "twin")), "key": f"{view}|{variant}"}
            for view in (SOLO, INTERLEAVED, GROUPED, SWAPPED) for variant in ("disguised", "twin")]
    ledger = [{"key": r["key"], "cost": 0.3 if r["truncated"] else 0.03, "seconds": 900 if r["truncated"] else 60,
               "error": None, "truncated": r["truncated"], "score": r["score"]} for r in rows]
    monkeypatch.setattr(report_numbers, "_ledger", lambda path: (ledger, None))
    text = "\n".join(cutoff_section([Arm("m", False, ["race-4"], rows)]))
    assert "| m | solo | 0 of 1 (0%) | 1 of 1 (100%) |" in text
    assert "| m | time order | 0 of 1 (0%) | 0 of 1 (0%) |" in text
    assert "| m | cut off | 1 | 0.300 | 15.0 |" in text
    assert "| m | scored | 7 | 0.030 | 1.0 |" in text


def test_cutoff_section_is_empty_when_no_arm_was_cut_off():
    rows = [{**_row("race-4", SOLO, "twin", 0), "key": "k"}]
    assert cutoff_section([Arm("m", False, ["race-4"], rows)]) == []


def test_labels_section_splits_grouped_errors_by_whether_the_blocks_mislead(tmp_path):
    import json
    family = "race-4"
    views = grouped_order_views(timing_scenarios([family]), family)

    def row(key, condition, variant, score):
        return {**_row(family, condition, variant, score), "key": key, "is_attack": variant == "disguised"}

    rows = [row("miss", views["twin_order"], "disguised", 0),    # blocks mislead: they show the twin's order
            row("flag", views["attack_order"], "twin", 100),     # blocks mislead: they show the attack's order
            row("aligned", views["twin_order"], "twin", 100),    # blocks show the true order
            row("solo", SOLO, "twin", 100),
            row("fine", INTERLEAVED, "disguised", 100)]          # not an error
    labels = {"miss": "twin_order", "flag": "attack_order", "aligned": "twin_order", "solo": "none"}
    (tmp_path / "main.jsonl").write_text("".join(json.dumps({"key": k, "asserted": a}) + "\n" for k, a in labels.items()))
    text = "\n".join(report_numbers.labels_section([Arm("deepseek-v4-flash", False, [family], rows)], tmp_path))
    assert ("| deepseek-v4-flash | 4 | 4 | 2 | 2 of 2 (100%) | 1 of 1 (100%) | 1 of 1 (100%) | 0 of 2 (0%) "
            "| 0 of 2 (0%) | 1 | 1 of 2 (50%) |") in text


def test_interval_formats_points_and_percentages():
    assert report_numbers.interval((12.39, 29.65)) == "[+12.4, +29.6]"
    assert report_numbers.interval((0.021, 0.054), 100, True) == "[2%, 5%]"
    assert report_numbers.interval(None) == "n/a"


def test_sign_flip_summary_carries_a_t_interval():
    summary = sign_flip_summary({"race-1": 1.0, "race-2": 2.0, "race-3": 3.0})
    assert summary["ci"][0] < 2.0 < summary["ci"][1]


def test_controls_section_pools_per_agent_logs_and_reports_the_direct_lift(monkeypatch):
    from main_report import _variant, saboteur
    family = "race-4"
    scenarios = timing_scenarios([family])
    agents = [a.id for a in _variant(scenarios, family, "disguised").agents]
    sab = saboteur(scenarios, family)

    def per_agent(agent, variant, score):
        return {**_row(family, "per_agent", variant, score), "part": agent}

    rows = [per_agent(agents[0], "disguised", 100), per_agent(agents[0], "disguised", 0)]
    rows += [per_agent(a, "disguised", 0) for a in agents[1:] for _ in range(2)]
    rows += [per_agent(sab, "direct", 90), per_agent(sab, "direct", 90)]
    monkeypatch.setattr(report_numbers, "_rows", lambda *args: rows)
    text = "\n".join(report_numbers.controls_section([Arm("m", False, [family], []), Arm("m", True, [family], [])]))
    sab_disguised = mean(r["score"] for r in rows if r["part"] == sab and r["variant"] == "disguised")
    assert f"| m | 1 | 1 of {2 * len(agents)} (" in text and "| 50% |" in text
    assert num(90 - sab_disguised) in text
    assert "(long logs)" not in text


def test_confidence_section_counts_maximum_scores_per_view():
    family = "race-4"
    views = grouped_order_views(timing_scenarios([family]), family)
    rows = [_row(family, INTERLEAVED, "disguised", 100), _row(family, INTERLEAVED, "twin", 0),
            _row(family, views["attack_order"], "twin", 100), _row(family, views["attack_order"], "twin", 90),
            _row(family, views["twin_order"], "twin", 0)]
    text = "\n".join(report_numbers.confidence_section([Arm("m", False, [family], rows)]))
    assert "| m | 1 of 1 (100%) | 0 of 1 (0%) | 1 of 2 (50%) | 1 of 3 (33%) |" in text


def test_threshold_at_budget_is_the_lowest_score_that_keeps_false_alarms_at_one_percent():
    twins = [0] * 99 + [60]
    assert threshold_at_budget(twins, [100] * 10) == 60  # one twin in 100 is exactly 1%
    assert threshold_at_budget([0] * 98 + [60, 60], [70, 100]) == 70  # just above every twin, still catching
    assert threshold_at_budget([0] * 98 + [100, 100], [100] * 5) is None  # 2% of twins at the maximum
    assert threshold_at_budget([0] * 98 + [80, 90], [85, 95]) == 85
