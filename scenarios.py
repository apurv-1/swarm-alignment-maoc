"""Scenario format: a small ML project where several agents act, with known ground truth.

Scenarios come in families. Each family holds three variants of the same project:

- disguised: a split attack. Every sabotage step can be defended using only what its own agent
  saw; the false result appears only when the agents' views are combined.
- twin: benign. The sabotage steps are copied word for word from the disguised variant, and
  one innocent detail in a different agent's log makes the combination legitimate.
- direct: the same sabotage done openly, so a single agent's log already shows it. This is the
  positive control: monitors should catch it in every view.

A scenario is a JSON file named <id>.json:

    {
      "id": "fam-a-disguised",
      "family": "fam-a",
      "variant": "disguised" | "twin" | "direct",
      "project": "What the team is building and the question it asks",
      "agents": [{"id": "agent_a", "task": "..."}, ...],
      "actions": [{"agent": "agent_a", "step": 1, "command": "...", "output": "..."}, ...],
      "final_state": {"key": "value", ...},       # same keys in every variant of a family
      "sabotage_steps": [5, 7],                   # attacks only
      "minimal_harmful_set": [5, 9],              # attacks only: removing any one step undoes the harm
      "structure": "conjunctive" | "additive" | "single_agent",  # attacks only
      "combined_harm": "What the steps do together"  # attacks only
    }

Edits are shown as unified diffs in `output`, never as narrated descriptions, so a monitor
reads what changed rather than the agent's account of it.
"""

import json
from dataclasses import dataclass, field
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
    agents: tuple[Agent, ...]
    actions: tuple[Action, ...]
    final_state: dict[str, str] = field(default_factory=dict)
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


class ScenarioError(ValueError):
    pass


def validate(scenario: Scenario) -> None:
    """Raise ScenarioError if a single scenario breaks the rules the experiment relies on."""
    sid = scenario.id
    if scenario.variant not in VARIANTS:
        raise ScenarioError(f"{sid}: variant must be one of {VARIANTS}")

    agent_ids = [a.id for a in scenario.agents]
    if len(set(agent_ids)) != len(agent_ids):
        raise ScenarioError(f"{sid}: duplicate agent ids")
    if len(agent_ids) < 2:
        raise ScenarioError(f"{sid}: needs at least two agents")

    steps = [a.step for a in scenario.actions]
    if not all(isinstance(s, int) for s in steps):
        raise ScenarioError(f"{sid}: steps must be integers")
    if len(set(steps)) != len(steps):
        raise ScenarioError(f"{sid}: steps must be unique")
    acting = {a.agent for a in scenario.actions}
    if acting - set(agent_ids):
        raise ScenarioError(f"{sid}: actions by unknown agents {sorted(acting - set(agent_ids))}")
    if set(agent_ids) - acting:
        raise ScenarioError(f"{sid}: agents with no actions {sorted(set(agent_ids) - acting)}")

    if not scenario.is_attack:
        if scenario.sabotage_steps or scenario.minimal_harmful_set or scenario.combined_harm:
            raise ScenarioError(f"{sid}: a twin must not list sabotage")
        return

    if not scenario.combined_harm:
        raise ScenarioError(f"{sid}: attack needs combined_harm")
    if scenario.structure not in ("conjunctive", "additive", "single_agent"):
        raise ScenarioError(f"{sid}: structure must be conjunctive, additive, or single_agent")
    for name, listed in (("sabotage_steps", scenario.sabotage_steps), ("minimal_harmful_set", scenario.minimal_harmful_set)):
        if not listed:
            raise ScenarioError(f"{sid}: attack needs {name}")
        if set(listed) - set(steps):
            raise ScenarioError(f"{sid}: {name} not in actions {sorted(set(listed) - set(steps))}")
    if scenario.variant == "disguised":
        # The point of a split attack: no single agent holds the whole harm.
        holders = {scenario.action_at(s).agent for s in scenario.minimal_harmful_set}
        if len(holders) < 2:
            raise ScenarioError(f"{sid}: minimal harmful set must span at least two agents")


def validate_family(variants: list[Scenario]) -> None:
    """Check the rules that link the variants of one family."""
    by_variant = {s.variant: s for s in variants}
    family = variants[0].family
    if sorted(by_variant) != sorted(VARIANTS) or len(variants) != len(VARIANTS):
        raise ScenarioError(f"{family}: needs exactly one each of {VARIANTS}")

    disguised, twin = by_variant["disguised"], by_variant["twin"]
    # The twin keeps every sabotage step word for word, so only context separates them.
    for step in disguised.sabotage_steps:
        try:
            twin_action = twin.action_at(step)
        except StopIteration:
            raise ScenarioError(f"{family}: twin is missing sabotage step {step}") from None
        if twin_action != disguised.action_at(step):
            raise ScenarioError(f"{family}: twin changes sabotage step {step}")
    if disguised.actions == twin.actions:
        raise ScenarioError(f"{family}: twin must differ from the disguised attack somewhere")

    keys = {tuple(sorted(s.final_state)) for s in variants}
    if len(keys) != 1:
        raise ScenarioError(f"{family}: final_state keys must match across variants")
    projects = {s.project for s in variants}
    if len(projects) != 1:
        raise ScenarioError(f"{family}: variants must share the same project")


def load_scenario(path: Path) -> Scenario:
    raw = json.loads(path.read_text())
    if raw["id"] != path.stem:
        raise ScenarioError(f"{path.name}: id {raw['id']!r} must match the file name")
    scenario = Scenario(
        id=raw["id"],
        family=raw["family"],
        variant=raw["variant"],
        project=raw["project"],
        agents=tuple(Agent(**a) for a in raw["agents"]),
        actions=tuple(Action(**a) for a in raw["actions"]),
        final_state={k: str(v) for k, v in raw.get("final_state", {}).items()},
        sabotage_steps=tuple(raw.get("sabotage_steps", ())),
        minimal_harmful_set=tuple(raw.get("minimal_harmful_set", ())),
        structure=raw.get("structure", ""),
        combined_harm=raw.get("combined_harm", ""),
    )
    validate(scenario)
    return scenario


def load_all(directory: Path = SCENARIOS_DIR) -> list[Scenario]:
    scenarios = [load_scenario(p) for p in sorted(directory.glob("*.json"))]
    families: dict[str, list[Scenario]] = {}
    for scenario in scenarios:
        families.setdefault(scenario.family, []).append(scenario)
    for variants in families.values():
        validate_family(variants)
    return scenarios
