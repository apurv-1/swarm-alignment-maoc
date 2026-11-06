import json

import pytest

from render import (
    SINGLE_TRANSCRIPT,
    format_action,
    per_agent,
    solo,
    team_diluted,
    team_interleaved,
    team_unlabelled,
)
from scenarios import Action, Agent, ScenarioError, load_all, load_scenario, validate

SCENARIOS = load_all()


def _action_blocks(scenario):
    """Command and output of every action, which must appear in every condition."""
    return [f"$ {a.command}\n{a.output}" for a in scenario.actions]


def test_scenarios_load_and_include_both_kinds():
    kinds = {s.kind for s in SCENARIOS}
    assert kinds == {"attack", "benign"}


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
@pytest.mark.parametrize("condition", sorted(SINGLE_TRANSCRIPT))
def test_every_condition_shows_every_action_exactly_once(scenario, condition):
    text = SINGLE_TRANSCRIPT[condition](scenario)
    for block in _action_blocks(scenario):
        assert text.count(block) == 1, f"{condition} lost or duplicated: {block[:60]}"


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
def test_per_agent_splits_actions_without_loss(scenario):
    transcripts = per_agent(scenario)
    assert len(transcripts) == len(scenario.agents)
    combined = "\n".join(transcripts)
    for block in _action_blocks(scenario):
        assert combined.count(block) == 1
    for agent, text in zip(scenario.agents, transcripts):
        for action in scenario.actions:
            assert (f"$ {action.command}\n{action.output}" in text) == (action.agent == agent.id)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
def test_solo_and_unlabelled_hide_agent_ids_in_actions(scenario):
    for text in (solo(scenario), team_unlabelled(scenario)):
        for agent in scenario.agents:
            assert agent.id not in text


def test_interleaved_is_in_time_order():
    scenario = SCENARIOS[0]
    text = team_interleaved(scenario)
    positions = [text.index(format_action(a, label=a.agent)) for a in scenario.actions_in_time_order()]
    assert positions == sorted(positions)


def test_diluted_adds_fillers_and_rejects_clashes():
    scenario = SCENARIOS[0]
    filler = (Agent("agent_x", "Write the README"), [Action("agent_x", 99, "edit README.md", "Edited README.md")])
    text = team_diluted(scenario, [filler])
    assert "agent_x" in text and "$ edit README.md\nEdited README.md" in text
    for block in _action_blocks(scenario):
        assert text.count(block) == 1
    with pytest.raises(ValueError):
        team_diluted(scenario, [(scenario.agents[0], [])])


def _write(tmp_path, **overrides):
    raw = {
        "id": "t",
        "kind": "attack",
        "project": "p",
        "reported_result": "r",
        "agents": [{"id": "a", "task": "x"}, {"id": "b", "task": "y"}],
        "actions": [
            {"agent": "a", "step": 1, "command": "c1", "output": "o1"},
            {"agent": "b", "step": 2, "command": "c2", "output": "o2"},
        ],
        "sabotage_steps": [1, 2],
        "combined_harm": "h",
    }
    raw.update(overrides)
    path = tmp_path / "s.json"
    path.write_text(json.dumps(raw))
    return path


def test_validation_accepts_a_split_attack(tmp_path):
    assert load_scenario(_write(tmp_path)).is_attack


@pytest.mark.parametrize(
    "overrides",
    [
        {"sabotage_steps": [1]},  # sabotage by one agent is not split
        {"sabotage_steps": [1, 9]},  # step that does not exist
        {"combined_harm": ""},  # attack without a stated harm
        {"kind": "benign"},  # benign scenario listing sabotage
        {"actions": [{"agent": "a", "step": 1, "command": "c", "output": "o"}]},  # idle agent
    ],
)
def test_validation_rejects_broken_scenarios(tmp_path, overrides):
    with pytest.raises(ScenarioError):
        load_scenario(_write(tmp_path, **overrides))


def test_real_scenarios_pass_validation():
    for scenario in SCENARIOS:
        validate(scenario)
