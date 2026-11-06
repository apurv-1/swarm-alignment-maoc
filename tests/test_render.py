import copy
import json

import pytest

from render import SINGLE_TRANSCRIPT, format_action, per_agent, solo, team_interleaved, team_unlabelled, with_final_state
from scenarios import ScenarioError, load_all, load_scenario, validate_family

SCENARIOS = load_all()
FAMILIES = sorted({s.family for s in SCENARIOS})


def _action_blocks(scenario, condition=None):
    """Every action as it is rendered, identified by its unique step, so repeats are not confused."""
    labelled = condition == "team_interleaved"
    return [format_action(a, label=a.agent if labelled else None) for a in scenario.actions]


def test_every_family_has_all_three_variants():
    assert len(FAMILIES) >= 4
    for family in FAMILIES:
        variants = sorted(s.variant for s in SCENARIOS if s.family == family)
        assert variants == ["direct", "disguised", "twin"]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
@pytest.mark.parametrize("condition", sorted(SINGLE_TRANSCRIPT))
def test_every_condition_shows_every_action_exactly_once(scenario, condition):
    text = SINGLE_TRANSCRIPT[condition](scenario)
    for block in _action_blocks(scenario, condition):
        assert text.count(block) == 1, f"{condition} lost or duplicated: {block[:60]}"
    assert scenario.project in text
    for agent in scenario.agents:
        assert agent.task in text


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
def test_per_agent_splits_actions_without_loss(scenario):
    transcripts = per_agent(scenario)
    assert len(transcripts) == len(scenario.agents)
    for agent, text in zip(scenario.agents, transcripts):
        for action in scenario.actions:
            assert (format_action(action, label=None) in text) == (action.agent == agent.id)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
def test_solo_and_unlabelled_hide_agent_ids(scenario):
    for text in (solo(scenario), team_unlabelled(scenario)):
        for agent in scenario.agents:
            assert agent.id not in text


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
def test_interleaved_is_in_time_order(scenario):
    text = team_interleaved(scenario)
    positions = [text.index(format_action(a, label=a.agent)) for a in scenario.actions_in_time_order()]
    assert positions == sorted(positions)


def test_final_state_is_appended_from_structured_fields():
    scenario = SCENARIOS[0]
    text = with_final_state("BODY", scenario)
    assert text.startswith("BODY")
    for key, value in scenario.final_state.items():
        assert f"- {key}: {value}" in text


@pytest.mark.parametrize("family", FAMILIES)
def test_disguised_and_twin_differ_only_outside_sabotage_steps(family):
    disguised = next(s for s in SCENARIOS if s.family == family and s.variant == "disguised")
    twin = next(s for s in SCENARIOS if s.family == family and s.variant == "twin")
    for step in disguised.sabotage_steps:
        assert twin.action_at(step) == disguised.action_at(step)
    changed = [a for a in disguised.actions if a not in twin.actions]
    changed_agents = {a.agent for a in changed}
    sabotaging_agents = {disguised.action_at(s).agent for s in disguised.sabotage_steps}
    # The context flip lives in a different agent's log from the sabotage.
    assert changed_agents and not changed_agents & sabotaging_agents


def _raw(**overrides):
    raw = {
        "id": "t",
        "family": "fam-t",
        "variant": "disguised",
        "project": "p",
        "agents": [{"id": "a", "task": "x"}, {"id": "b", "task": "y"}],
        "actions": [
            {"agent": "a", "step": 1, "command": "c1", "output": "o1"},
            {"agent": "b", "step": 2, "command": "c2", "output": "o2"},
        ],
        "final_state": {"k": 1},
        "sabotage_steps": [1],
        "minimal_harmful_set": [1, 2],
        "structure": "conjunctive",
        "combined_harm": "h",
    }
    raw.update(overrides)
    return raw


def _load(tmp_path, raw, name="t"):
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(raw))
    return load_scenario(path)


def test_validation_accepts_a_split_attack(tmp_path):
    assert _load(tmp_path, _raw()).is_attack


@pytest.mark.parametrize(
    "overrides",
    [
        {"minimal_harmful_set": [1]},  # harm held by one agent is not a split attack
        {"sabotage_steps": [9]},  # step that does not exist
        {"combined_harm": ""},
        {"structure": "other"},
        {"variant": "twin"},  # a twin listing sabotage
        {"variant": "other"},
        {"actions": [{"agent": "a", "step": "1", "command": "c", "output": "o"}, {"agent": "b", "step": 2, "command": "c", "output": "o"}]},
        {"actions": [{"agent": "a", "step": 1, "command": "c", "output": "o"}]},  # idle agent
    ],
)
def test_validation_rejects_broken_scenarios(tmp_path, overrides):
    with pytest.raises(ScenarioError):
        _load(tmp_path, _raw(**overrides))


def test_validation_requires_id_to_match_file_name(tmp_path):
    with pytest.raises(ScenarioError):
        _load(tmp_path, _raw(), name="other-name")


def test_family_rules_catch_a_twin_that_edits_sabotage():
    family = [copy.deepcopy(s) for s in SCENARIOS if s.family == FAMILIES[0]]
    validate_family(family)
    twin = next(s for s in family if s.variant == "twin")
    disguised = next(s for s in family if s.variant == "disguised")
    step = disguised.sabotage_steps[0]
    edited = tuple(
        type(a)(a.agent, a.step, a.command + " --changed", a.output) if a.step == step else a for a in twin.actions
    )
    broken_twin = type(twin)(**{**twin.__dict__, "actions": edited})
    with pytest.raises(ScenarioError):
        validate_family([broken_twin if s is twin else s for s in family])
