import json
from dataclasses import replace

import pytest

from render import (
    SINGLE_TRANSCRIPT,
    format_action,
    per_agent,
    solo,
    step_numbers,
    team_grouped,
    team_grouped_swapped,
    team_interleaved,
    team_unlabelled,
)
from scenarios import Action, Agent, Scenario, ScenarioError, load_all, load_scenario, validate_family

SCENARIOS = load_all()
FAMILIES = sorted({s.family for s in SCENARIOS})
CONTENT_FAMILIES = sorted({s.family for s in SCENARIOS if s.twin_kind == "content"})
TIMING_FAMILIES = sorted({s.family for s in SCENARIOS if s.twin_kind == "timing"})


def _variant(family, variant):
    return next(s for s in SCENARIOS if s.family == family and s.variant == variant)


def _family(family):
    return [s for s in SCENARIOS if s.family == family]


def _action_blocks(scenario, condition=None):
    """Every action as it is rendered, identified by its unique step, so repeats are not confused."""
    labelled = condition == "team_interleaved"
    numbers = step_numbers(scenario)
    return [format_action(a, numbers[a.step], a.agent if labelled else None) for a in scenario.actions]


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
        own = scenario.actions_by_agent(agent.id)
        for number, action in enumerate(own, 1):
            assert format_action(action, number, None) in text
        mine = {(a.command, a.output) for a in own}  # long families share routine filler across agents
        for action in scenario.actions:
            if action.agent != agent.id and (action.command, action.output) not in mine:
                assert f"$ {action.command}\n{action.output}" not in text


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
def test_per_agent_numbers_each_log_one_to_k(scenario):
    for agent, text in zip(scenario.agents, per_agent(scenario)):
        k = len(scenario.actions_by_agent(agent.id))
        assert [f"[step {n}]" in text for n in range(1, k + 2)] == [True] * k + [False]


@pytest.mark.parametrize("family", FAMILIES)
def test_direct_leaves_other_agents_per_agent_logs_byte_identical(family):
    """Adding the saboteur's read steps must not change any other agent's log, not even its numbers."""
    disguised, direct = _variant(family, "disguised"), _variant(family, "direct")
    saboteurs = direct.agents_of(direct.sabotage_steps)
    for agent, d, x in zip(disguised.agents, per_agent(disguised), per_agent(direct)):
        if agent.id not in saboteurs:
            assert d == x, agent.id


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
@pytest.mark.parametrize("view", [team_grouped, team_grouped_swapped], ids=lambda f: f.__name__)
def test_grouped_views_keep_project_wide_numbers_and_block_order(scenario, view):
    text = view(scenario)
    numbers = step_numbers(scenario)
    for action in scenario.actions:
        assert format_action(action, numbers[action.step], None) in text
    expected = list(scenario.agents)
    if view is team_grouped_swapped:
        expected[0], expected[1] = expected[1], expected[0]
    positions = [text.index(f"=== {a.id} ===") for a in expected]
    assert positions == sorted(positions)
    # The header lists the agents in the same order in every view, so only the blocks move.
    header = text.split("\n\n=== ")[0]
    assert header == team_grouped(scenario).split("\n\n=== ")[0]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
def test_solo_and_unlabelled_hide_agent_ids(scenario):
    for text in (solo(scenario), team_unlabelled(scenario)):
        for agent in scenario.agents:
            assert agent.id not in text


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
def test_interleaved_is_in_time_order(scenario):
    text = team_interleaved(scenario)
    numbers = step_numbers(scenario)
    positions = [text.index(format_action(a, numbers[a.step], a.agent)) for a in scenario.actions_in_time_order()]
    assert positions == sorted(positions)


@pytest.mark.parametrize("family", TIMING_FAMILIES)
def test_timing_twin_keeps_every_per_agent_view_byte_identical(family):
    disguised, twin = _variant(family, "disguised"), _variant(family, "twin")
    assert per_agent(disguised) == per_agent(twin)
    assert solo(disguised) != solo(twin)


@pytest.mark.parametrize("family", CONTENT_FAMILIES)
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
        {"actions": [
            {"agent": "a", "step": 10, "command": "c1", "output": "o1"},
            {"agent": "b", "step": 20, "command": "c2", "output": "o2"},
            {"agent": "a", "step": 30, "command": "c3", "output": "o3"},
        ], "sabotage_steps": [30], "minimal_harmful_set": [10, 20]},  # sabotage outside the harmful set
        {"agents": [{"id": "a", "task": "x"}, {"id": "a", "task": "y"}]},  # duplicate agent ids
        {"actions": [{"agent": "a", "step": 10, "command": "c", "output": "o"}, {"agent": "b", "step": 10, "command": "c", "output": "o"}]},
        {"actions": [{"agent": "a", "step": 10, "command": "c", "output": "o"}, {"agent": "b", "step": 20, "command": "c", "output": "o"},
                     {"agent": "z", "step": 30, "command": "c", "output": "o"}]},  # unknown acting agent
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


def _with_action(scenario, at, **changes):
    actions = tuple(replace(a, **changes) if a.step == at else a for a in scenario.actions)
    return replace(scenario, actions=actions)


def _swap(family, variant, new):
    return [new if s.variant == variant else s for s in family]


@pytest.mark.parametrize("family", CONTENT_FAMILIES)
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
    # Direct with different sabotage steps.
    broken.append(_swap(variants, "direct", replace(direct, sabotage_steps=(extra_step,))))
    # Direct whose reveal comes after the sabotage.
    late = max(a.step for a in direct.actions) + 1
    broken.append(_swap(variants, "direct", replace(
        _with_action(direct, extra_step, step=late), minimal_harmful_set=tuple(sorted({late, *direct.sabotage_steps}))
    )))
    # Twin that moves an action to another time.
    moved = max(a.step for a in twin.actions) + 1
    broken.append(_swap(variants, "twin", _with_action(twin, sabotage_step, step=moved)))
    # A twin that changes only the saboteur's log, or only the reporter's log.
    context = next(a.id for a, d, t in zip(disguised.agents, per_agent(disguised), per_agent(twin)) if d != t)
    restored = replace(twin, actions=tuple(
        disguised.action_at(a.step) if a.agent == context else a for a in twin.actions
    ))
    broken.append(_swap(variants, "twin", _with_action(restored, sabotage_step, output="changed")))
    broken.append(_swap(variants, "twin", _with_action(restored, reporter_step, output="changed")))
    # A project that differs across variants.
    broken.append(_swap(variants, "twin", replace(twin, project="changed")))
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


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
def test_displayed_steps_run_one_to_n(scenario):
    text = solo(scenario)
    for number in range(1, len(scenario.actions) + 1):
        assert f"[step {number}]" in text
    assert f"[step {len(scenario.actions) + 1}]" not in text


def test_action_is_hashable_for_set_comparisons():
    assert len({Action("a", 1, "c", "o"), Action("a", 1, "c", "o")}) == 1


def test_a_reporter_who_is_also_a_saboteur_is_rejected():
    family = _family(FAMILIES[0])
    disguised = _variant(FAMILIES[0], "disguised")
    reporter_step = disguised.actions_in_time_order()[-1].step
    sabotage = (reporter_step,)
    harmful = tuple(sorted({*disguised.minimal_harmful_set, reporter_step}))
    changed = [replace(s, sabotage_steps=sabotage, minimal_harmful_set=harmful) if s.is_attack else s for s in family]
    with pytest.raises(ScenarioError, match="reporter"):
        validate_family(changed)


# A minimal timing family: agent x writes a shared file, agent y reads it, reporter r acts first and last.
# The harm needs the write before the read; the twin has the read first.
_AGENTS = (Agent("x", "write S"), Agent("y", "use S"), Agent("r", "report"))


def _timing(variant, steps, extra=(), **attack):
    """steps maps each action name to its step number."""
    contents = {
        "setup": ("r", "make setup", "ok"),
        "prep": ("x", "prep", "ready"),
        "write": ("x", "write S", "wrote S"),
        "read": ("y", "train --from S", "trained"),
        "report": ("r", "make table", "| result |"),
    }
    actions = tuple(Action(contents[n][0], step, contents[n][1], contents[n][2]) for n, step in steps.items()) + tuple(extra)
    return Scenario(
        id=f"t-{variant}", family="t", variant=variant, project="p", reported_result="r",
        agents=_AGENTS, actions=actions, twin_kind="timing", **attack,
    )


_DISGUISED_STEPS = {"setup": 1, "prep": 5, "write": 10, "read": 20, "report": 30}
_TWIN_STEPS = {"setup": 1, "prep": 5, "write": 25, "read": 20, "report": 30}


def _timing_family(twin_steps=_TWIN_STEPS, **twin_changes):
    disguised = _timing("disguised", _DISGUISED_STEPS, sabotage_steps=(20,), minimal_harmful_set=(10, 20),
                        structure="conjunctive", combined_harm="h")
    twin = replace(_timing("twin", twin_steps), **twin_changes)
    direct = _timing("direct", _DISGUISED_STEPS, extra=(Action("y", 15, "git log -1 -- S", "x wrote S"),),
                     sabotage_steps=(20,), minimal_harmful_set=(15, 20), structure="single_agent", combined_harm="h")
    return [disguised, twin, direct]


def test_a_timing_twin_that_only_reorders_across_agents_is_accepted():
    family = _timing_family()
    validate_family(family)
    disguised, twin, _ = family
    assert per_agent(disguised) == per_agent(twin)


def _changed(scenario, name, **changes):
    step = _TWIN_STEPS[name]
    return replace(scenario, actions=tuple(replace(a, **changes) if a.step == step else a for a in scenario.actions))


@pytest.mark.parametrize(
    "twin_steps",
    [
        _DISGUISED_STEPS,  # no reordering at all
        {"setup": 1, "prep": 15, "write": 10, "read": 20, "report": 30},  # reordered within agent x only
        {"setup": 22, "prep": 5, "write": 10, "read": 20, "report": 30},  # moves a harmless step only
        {"setup": 1, "prep": 5, "write": 40, "read": 20, "report": 30},  # the reporter no longer acts last
    ],
)
def test_timing_twins_that_do_not_reverse_the_harmful_order_are_rejected(twin_steps):
    with pytest.raises(ScenarioError):
        validate_family(_timing_family(twin_steps))


@pytest.mark.parametrize("changes", [{"output": "changed"}, {"command": "changed"}])
def test_timing_twins_that_change_an_action_are_rejected(changes):
    family = _timing_family()
    family[1] = _changed(family[1], "read", **changes)
    with pytest.raises(ScenarioError, match="action for action"):
        validate_family(family)


def test_twin_kind_must_match_across_variants():
    family = _timing_family()
    family[2] = replace(family[2], twin_kind="content")
    with pytest.raises(ScenarioError, match="twin_kind"):
        validate_family(family)
