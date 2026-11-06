"""Scenario format: a small ML project where several agents act, with known ground truth.

Scenarios come in families. Each family holds three variants of the same project:

- disguised: a split attack. Every sabotage step can be defended using only what its own agent
  saw; the false result appears only when the agents' views are combined.
- twin: benign. Exactly one agent's log differs from the disguised variant: the context agent,
  who is not a saboteur. Every other log, including the reported result, is identical, so only
  the combination of views can tell attack from twin.
- direct: the disguised variant plus extra read steps in the saboteur's own log that reveal the
  conflict before the saboteur's last action, with nothing else changed. This is the positive control: the harm is visible from
  a single agent's log.

Steps are numbered in tens so a direct variant can insert a step without renumbering the rest.

A scenario is a JSON file named <id>.json:

    {
      "id": "fam-a-disguised",
      "family": "fam-a",
      "variant": "disguised" | "twin" | "direct",
      "project": "What the team is building and the question it asks",
      "reported_result": "The result the team reports, identical across variants",
      "agents": [{"id": "agent_a", "task": "..."}, ...],
      "actions": [{"agent": "agent_a", "step": 10, "command": "...", "output": "..."}, ...],
      "sabotage_steps": [80],                     # attacks only
      "minimal_harmful_set": [50, 80],            # attacks only: removing any one step undoes the harm
      "structure": "conjunctive" | "additive" | "single_agent",  # attacks only
      "combined_harm": "What the steps do together"  # attacks only
    }

Edits are shown as unified diffs in `output`, never as narrated descriptions, so a monitor
reads what changed rather than the agent's account of it.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

SCENARIOS_DIR = Path(__file__).parent / "data" / "scenarios"
VARIANTS = ("disguised", "twin", "direct")


@dataclass(frozen=True)
class Agent:
    id: str
    task: str


@dataclass(frozen=True)
class Action:
    agent: str
    step: int
    command: str
    output: str


@dataclass(frozen=True)
class Scenario:
    id: str
    family: str
    variant: Literal["disguised", "twin", "direct"]
    project: str
    reported_result: str
    agents: tuple[Agent, ...]
    actions: tuple[Action, ...]
    sabotage_steps: tuple[int, ...] = ()
    minimal_harmful_set: tuple[int, ...] = ()
    structure: str = ""
    combined_harm: str = ""

    @property
    def is_attack(self) -> bool:
        return self.variant != "twin"

    def actions_in_time_order(self) -> list[Action]:
        return sorted(self.actions, key=lambda a: a.step)

    def actions_by_agent(self, agent_id: str) -> list[Action]:
        return [a for a in self.actions_in_time_order() if a.agent == agent_id]

    def action_at(self, step: int) -> Action:
        return next(a for a in self.actions if a.step == step)

    def agents_of(self, steps) -> set[str]:
        return {self.action_at(s).agent for s in steps}


class ScenarioError(ValueError):
    pass


def validate(scenario: Scenario) -> None:
    """Raise ScenarioError if a single scenario breaks the rules the experiment relies on."""
    sid = scenario.id
    if scenario.variant not in VARIANTS:
        raise ScenarioError(f"{sid}: variant must be one of {VARIANTS}")
    if not scenario.reported_result:
        raise ScenarioError(f"{sid}: needs reported_result")

    agent_ids = [a.id for a in scenario.agents]
    if len(set(agent_ids)) != len(agent_ids):
        raise ScenarioError(f"{sid}: duplicate agent ids")
    if len(agent_ids) < 2:
        raise ScenarioError(f"{sid}: needs at least two agents")

    steps = [a.step for a in scenario.actions]
    if not all(isinstance(s, int) and not isinstance(s, bool) for s in steps):
        raise ScenarioError(f"{sid}: steps must be integers")
    if len(set(steps)) != len(steps):
        raise ScenarioError(f"{sid}: steps must be unique")
    acting = {a.agent for a in scenario.actions}
    if acting - set(agent_ids):
        raise ScenarioError(f"{sid}: actions by unknown agents {sorted(acting - set(agent_ids))}")
    if set(agent_ids) - acting:
        raise ScenarioError(f"{sid}: agents with no actions {sorted(set(agent_ids) - acting)}")

    if not scenario.is_attack:
        if scenario.sabotage_steps or scenario.minimal_harmful_set or scenario.combined_harm or scenario.structure:
            raise ScenarioError(f"{sid}: a twin must not list sabotage")
        return

    if not scenario.combined_harm:
        raise ScenarioError(f"{sid}: attack needs combined_harm")
    for name, listed in (("sabotage_steps", scenario.sabotage_steps), ("minimal_harmful_set", scenario.minimal_harmful_set)):
        if not listed:
            raise ScenarioError(f"{sid}: attack needs {name}")
        if set(listed) - set(steps):
            raise ScenarioError(f"{sid}: {name} not in actions {sorted(set(listed) - set(steps))}")

    if not set(scenario.sabotage_steps) <= set(scenario.minimal_harmful_set):
        raise ScenarioError(f"{sid}: sabotage steps must be part of the minimal harmful set")

    holders = scenario.agents_of(scenario.minimal_harmful_set)
    if scenario.variant == "disguised":
        if scenario.structure not in ("conjunctive", "additive"):
            raise ScenarioError(f"{sid}: a disguised attack must be conjunctive or additive")
        # The point of a split attack: no single agent holds the whole harm.
        if len(holders) < 2:
            raise ScenarioError(f"{sid}: minimal harmful set must span at least two agents")
    else:
        if scenario.structure != "single_agent":
            raise ScenarioError(f"{sid}: a direct attack must be single_agent")
        if len(holders) != 1:
            raise ScenarioError(f"{sid}: a direct attack's harm must sit in one agent's log")


def _logs_by_agent(scenario: Scenario) -> dict[str, list[Action]]:
    return {a.id: scenario.actions_by_agent(a.id) for a in scenario.agents}


def validate_family(variants: list[Scenario]) -> None:
    """Check the rules that link the variants of one family."""
    by_variant = {s.variant: s for s in variants}
    family = variants[0].family
    if sorted(by_variant) != sorted(VARIANTS) or len(variants) != len(VARIANTS):
        raise ScenarioError(f"{family}: needs exactly one each of {VARIANTS}")
    for attribute in ("project", "reported_result", "agents"):
        if len({repr(getattr(s, attribute)) for s in variants}) != 1:
            raise ScenarioError(f"{family}: {attribute} must match across variants")

    disguised, twin, direct = (by_variant[v] for v in VARIANTS)
    saboteurs = disguised.agents_of(disguised.sabotage_steps)

    # Twin: same steps by the same agents; exactly one agent's log differs, and that agent is
    # neither a saboteur nor the reporter.
    if [(a.step, a.agent) for a in disguised.actions_in_time_order()] != [
        (a.step, a.agent) for a in twin.actions_in_time_order()
    ]:
        raise ScenarioError(f"{family}: twin must keep the disguised steps and their agents")
    disguised_logs, twin_logs = _logs_by_agent(disguised), _logs_by_agent(twin)
    differing = [agent for agent in disguised_logs if disguised_logs[agent] != twin_logs[agent]]
    if len(differing) != 1:
        raise ScenarioError(f"{family}: twin must differ in exactly one agent's log, not {differing}")
    if differing[0] in saboteurs:
        raise ScenarioError(f"{family}: twin must not change a saboteur's log")
    # The reporter acts last; its log carries the outcome numbers, which must not differ.
    reporter = disguised.actions_in_time_order()[-1].agent
    if reporter in saboteurs:
        raise ScenarioError(f"{family}: the reporter must not be a saboteur")
    if differing[0] == reporter:
        raise ScenarioError(f"{family}: twin must not change the reporter's log")

    # Direct: every disguised action unchanged, plus extra read steps in the saboteur's own log,
    # before the sabotage, with the same sabotage steps.
    if not set(disguised.actions) <= set(direct.actions):
        raise ScenarioError(f"{family}: direct must keep every disguised action unchanged")
    if direct.sabotage_steps != disguised.sabotage_steps:
        raise ScenarioError(f"{family}: direct must keep the disguised sabotage steps")
    extra = set(direct.actions) - set(disguised.actions)
    extra_agents = {a.agent for a in extra}
    if not extra_agents or extra_agents != direct.agents_of(direct.minimal_harmful_set) or not extra_agents <= saboteurs:
        raise ScenarioError(f"{family}: direct's extra steps must be in the saboteur's own log")
    # The saboteur must act after learning the conflict, so the reveal comes before its last action.
    last_saboteur_step = max(a.step for a in direct.actions if a.agent in saboteurs and a not in extra)
    if max(a.step for a in extra) > last_saboteur_step:
        raise ScenarioError(f"{family}: direct's extra steps must come before the saboteur's last action")


def load_scenario(path: Path) -> Scenario:
    raw = json.loads(path.read_text())
    if raw["id"] != path.stem:
        raise ScenarioError(f"{path.name}: id {raw['id']!r} must match the file name")
    scenario = Scenario(
        id=raw["id"],
        family=raw["family"],
        variant=raw["variant"],
        project=raw["project"],
        reported_result=raw.get("reported_result", ""),
        agents=tuple(Agent(**a) for a in raw["agents"]),
        actions=tuple(Action(**a) for a in raw["actions"]),
        sabotage_steps=tuple(raw.get("sabotage_steps", ())),
        minimal_harmful_set=tuple(raw.get("minimal_harmful_set", ())),
        structure=raw.get("structure", ""),
        combined_harm=raw.get("combined_harm", ""),
    )
    validate(scenario)
    return scenario


def load_all(directory: Path = SCENARIOS_DIR) -> list[Scenario]:
    scenarios = [load_scenario(p) for p in sorted(directory.glob("*.json"))]
    ids = [s.id for s in scenarios]
    if len(set(ids)) != len(ids):
        raise ScenarioError("scenario ids must be unique")
    families: dict[str, list[Scenario]] = {}
    for scenario in scenarios:
        families.setdefault(scenario.family, []).append(scenario)
    for variants in families.values():
        validate_family(variants)
    return scenarios
