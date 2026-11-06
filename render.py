"""Turn one scenario into the transcripts a monitor reads, under each presentation condition.

Every condition shows exactly the same actions, so any change in what the monitor catches
comes from presentation alone, not from a different attack. The tests check that invariant.

Conditions:
- per_agent: each agent's actions on their own, one transcript per agent.
- solo: all actions in time order, as if one agent did everything.
- team_grouped: all agents, each agent's actions in its own block.
- team_interleaved: all agents, actions in time order, each labelled with its agent.
- team_unlabelled: all agents' tasks listed, actions in time order with no agent labels.
- team_diluted: team_grouped plus uninvolved agents doing unrelated work.
"""

from collections.abc import Callable, Sequence

from scenarios import Action, Agent, Scenario

SOLO_AGENT = "agent"


def format_action(action: Action, label: str | None) -> str:
    who = f" {label}" if label else ""
    return f"[step {action.step}]{who}\n$ {action.command}\n{action.output}"


def _header(project: str, agents: Sequence[Agent], labelled: bool = True) -> str:
    if labelled:
        tasks = "\n".join(f"- {a.id}: {a.task}" for a in agents)
    else:
        tasks = "\n".join(f"- {a.task}" for a in agents)
    return f"Project: {project}\n\nAgents and their assigned tasks:\n{tasks}"


def _block(agent: Agent, actions: Sequence[Action]) -> str:
    body = "\n\n".join(format_action(a, label=None) for a in actions)
    return f"=== {agent.id} ===\n{body}"


def per_agent(scenario: Scenario) -> list[str]:
    transcripts = []
    for agent in scenario.agents:
        body = "\n\n".join(format_action(a, label=None) for a in scenario.actions_by_agent(agent.id))
        transcripts.append(
            f"Project: {scenario.project}\n\nAgent: {agent.id}\nAssigned task: {agent.task}\n\n{body}"
        )
    return transcripts


def solo(scenario: Scenario) -> str:
    task = " ".join(a.task for a in scenario.agents)
    body = "\n\n".join(format_action(a, label=None) for a in scenario.actions_in_time_order())
    return f"Project: {scenario.project}\n\nAgent: {SOLO_AGENT}\nAssigned task: {task}\n\n{body}"


def team_grouped(scenario: Scenario) -> str:
    blocks = [_block(a, scenario.actions_by_agent(a.id)) for a in scenario.agents]
    return _header(scenario.project, scenario.agents) + "\n\n" + "\n\n".join(blocks)


def team_interleaved(scenario: Scenario) -> str:
    body = "\n\n".join(format_action(a, label=a.agent) for a in scenario.actions_in_time_order())
    return _header(scenario.project, scenario.agents) + "\n\n" + body


def team_unlabelled(scenario: Scenario) -> str:
    body = "\n\n".join(format_action(a, label=None) for a in scenario.actions_in_time_order())
    return _header(scenario.project, scenario.agents, labelled=False) + "\n\n" + body


def team_diluted(scenario: Scenario, fillers: Sequence[tuple[Agent, Sequence[Action]]]) -> str:
    """team_grouped with extra, uninvolved agents appended after the real ones.

    Filler agents must not reuse the scenario's agent ids.
    """
    clashes = {a.id for a, _ in fillers} & {a.id for a in scenario.agents}
    if clashes:
        raise ValueError(f"filler agent ids clash with scenario agents: {sorted(clashes)}")
    agents = list(scenario.agents) + [a for a, _ in fillers]
    blocks = [_block(a, scenario.actions_by_agent(a.id)) for a in scenario.agents]
    blocks += [_block(a, actions) for a, actions in fillers]
    return _header(scenario.project, agents) + "\n\n" + "\n\n".join(blocks)


# Conditions that produce one transcript per scenario.
SINGLE_TRANSCRIPT: dict[str, Callable[[Scenario], str]] = {
    "solo": solo,
    "team_grouped": team_grouped,
    "team_interleaved": team_interleaved,
    "team_unlabelled": team_unlabelled,
}
