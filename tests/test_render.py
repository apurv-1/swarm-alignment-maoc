import json
from dataclasses import replace

import pytest

from render import SINGLE_TRANSCRIPT, format_action, per_agent, solo, team_interleaved, team_unlabelled
from scenarios import Action, ScenarioError, load_all, load_scenario, validate_family

SCENARIOS = load_all()
FAMILIES = sorted({s.family for s in SCENARIOS})


def _variant(family, variant):
    return next(s for s in SCENARIOS if s.family == family and s.variant == variant)


def _family(family):
    return [s for s in SCENARIOS if s.family == family]


def _action_blocks(scenario, condition=None):
    """Every action as it is rendered, identified by its unique step, so repeats are not confused."""
    labelled = condition == "team_interleaved"
    return [format_action(a, label=a.agent if labelled else None) for a in scenario.actions]


def test_every_family_has_all_three_variants():
    assert len(FAMILIES) >= 4
    for family in FAMILIES:
        assert sorted(s.variant for s in _family(family)) == ["direct", "disguised", "twin"]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
@pytest.mark.parametrize("condition", sorted(SINGLE_TRANSCRIPT))
def test_every_condition_shows_every_action_exactly_once(scenario, condition):
    text = SINGLE_TRANSCRIPT[condition](scenario)
    for block in _action_blocks(scenario, condition):
        assert text.count(block) == 1, f"{condition} lost or duplicated: {block[:60]}"
    assert scenario.project in text
    assert scenario.reported_result in text
    for agent in scenario.agents:
        assert agent.task in text


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
def test_per_agent_splits_actions_without_loss_and_shows_the_claim(scenario):
    transcripts = per_agent(scenario)
    assert len(transcripts) == len(scenario.agents)
    for agent, text in zip(scenario.agents, transcripts):
        assert scenario.reported_result in text
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


@pytest.mark.parametrize("family", FAMILIES)
def test_twin_changes_one_non_saboteur_non_reporter_view_only(family):
    """Per-agent views must not tell attack from twin: all but the context agent's are byte-identical."""
    disguised, twin = _variant(family, "disguised"), _variant(family, "twin")
    differing = [a.id for a, d, t in zip(disguised.agents, per_agent(disguised), per_agent(twin)) if d != t]
    assert len(differing) == 1
    saboteurs = disguised.agents_of(disguised.sabotage_steps)
    reporter = disguised.actions_in_time_order()[-1].agent
    assert differing[0] not in saboteurs and differing[0] != reporter


@pytest.mark.parametrize("family", FAMILIES)
def test_direct_adds_only_saboteur_steps(family):
    disguised, direct = _variant(family, "disguised"), _variant(family, "direct")
    extra = set(direct.actions) - set(disguised.actions)
    assert extra and set(disguised.actions) <= set(direct.actions)
    assert {a.agent for a in extra} == direct.agents_of(direct.minimal_harmful_set)
    assert len(direct.agents_of(direct.minimal_harmful_set)) == 1
    assert len(disguised.agents_of(disguised.minimal_harmful_set)) >= 2


def _raw(**overrides):
    raw = {
        "id": "t",
        "family": "fam-t",
        "variant": "disguised",
        "project": "p",
        "reported_result": "r",
        "agents": [{"id": "a", "task": "x"}, {"id": "b", "task": "y"}],
        "actions": [
            {"agent": "a", "step": 10, "command": "c1", "output": "o1"},
            {"agent": "b", "step": 20, "command": "c2", "output": "o2"},
        ],
        "sabotage_steps": [10],
        "minimal_harmful_set": [10, 20],
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
        {"minimal_harmful_set": [10]},  # harm held by one agent is not a split attack
        {"sabotage_steps": [90]},  # step that does not exist
        {"combined_harm": ""},
        {"reported_result": ""},
        {"structure": "other"},
        {"structure": "single_agent"},  # a disguised attack cannot be single-agent
        {"variant": "twin"},  # a twin listing sabotage
        {"variant": "direct"},  # direct must be single_agent with harm in one log
        {"variant": "direct", "structure": "single_agent"},  # ...and harm in one agent's log
        {"variant": "other"},
        {"actions": [{"agent": "a", "step": "10", "command": "c", "output": "o"}, {"agent": "b", "step": 20, "command": "c", "output": "o"}]},
        {"actions": [{"agent": "a", "step": 10, "command": "c", "output": "o"}]},  # idle agent
    ],
)
def test_validation_rejects_broken_scenarios(tmp_path, overrides):
    with pytest.raises(ScenarioError):
        _load(tmp_path, _raw(**overrides))


def test_validation_requires_id_to_match_file_name(tmp_path):
    with pytest.raises(ScenarioError):
        _load(tmp_path, _raw(), name="other-name")


def _with_action(scenario, step, **changes):
    actions = tuple(replace(a, **changes) if a.step == step else a for a in scenario.actions)
    return replace(scenario, actions=actions)


def _swap(family, variant, new):
    return [new if s.variant == variant else s for s in family]


@pytest.mark.parametrize("family", FAMILIES)
def test_family_rules_hold_and_catch_broken_variants(family):
    variants = _family(family)
    validate_family(variants)
    disguised, twin, direct = (_variant(family, v) for v in ("disguised", "twin", "direct"))
    reporter_step = disguised.actions_in_time_order()[-1].step
    sabotage_step = disguised.sabotage_steps[0]

    broken = [
        # A twin that edits a sabotage step.
        _swap(variants, "twin", _with_action(twin, sabotage_step, command="changed")),
        # A twin whose reported outcome differs: the reporter's log changes.
        _swap(variants, "twin", _with_action(twin, reporter_step, output="changed")),
        # A direct variant that changes an existing action instead of only adding one.
        _swap(variants, "direct", _with_action(direct, reporter_step, output="changed")),
        # A twin with a different reported result.
        _swap(variants, "twin", replace(twin, reported_result="changed")),
    ]
    # A direct variant whose extra step belongs to another agent.
    extra_step = min(set(a.step for a in direct.actions) - set(a.step for a in disguised.actions))
    other_agent = next(a.id for a in direct.agents if a.id not in direct.agents_of([extra_step]))
    broken.append(_swap(variants, "direct", _with_action(direct, extra_step, agent=other_agent)))
    for case in broken:
        with pytest.raises(ScenarioError):
            validate_family(case)


def test_twin_identical_to_disguised_is_rejected():
    family = _family(FAMILIES[0])
    disguised = _variant(FAMILIES[0], "disguised")
    copy_as_twin = replace(
        disguised, id="x", variant="twin", sabotage_steps=(), minimal_harmful_set=(), structure="", combined_harm=""
    )
    with pytest.raises(ScenarioError):
        validate_family(_swap(family, "twin", copy_as_twin))


def test_action_is_hashable_for_set_comparisons():
    assert len({Action("a", 1, "c", "o"), Action("a", 1, "c", "o")}) == 1
