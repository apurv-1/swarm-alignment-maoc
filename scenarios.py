"""Scenario format: a small ML project where several agents act, with known ground truth.

A scenario is a JSON file. Attack scenarios hide one sabotage step per agent that together
produce a false result; benign scenarios contain look-alike changes that are legitimate.

    {
      "id": "sab-001",
      "kind": "attack" or "benign",
      "project": "What the team is building and the question it asks",
      "reported_result": "The headline result the team reports at the end",
      "agents": [{"id": "agent_a", "task": "The work this agent was assigned"}, ...],
      "actions": [{"agent": "agent_a", "step": 1, "command": "...", "output": "..."}, ...],
      "sabotage_steps": [3, 7, 12],            # attack only: steps that are sabotage
      "combined_harm": "What the steps do together"   # attack only
    }

`step` gives the global time order across agents, so the same actions can be shown grouped by
agent, interleaved in time, or as if one agent did everything.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

SCENARIOS_DIR = Path(__file__).parent / "data" / "scenarios"


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
    kind: Literal["attack", "benign"]
    project: str
    reported_result: str
    agents: tuple[Agent, ...]
    actions: tuple[Action, ...]
    sabotage_steps: tuple[int, ...] = ()
    combined_harm: str = ""

    @property
    def is_attack(self) -> bool:
        return self.kind == "attack"

    def actions_in_time_order(self) -> list[Action]:
        return sorted(self.actions, key=lambda a: a.step)

    def actions_by_agent(self, agent_id: str) -> list[Action]:
        return [a for a in self.actions_in_time_order() if a.agent == agent_id]


class ScenarioError(ValueError):
    pass


def validate(scenario: Scenario) -> None:
    """Raise ScenarioError if the scenario breaks the rules the experiment relies on."""
    agent_ids = [a.id for a in scenario.agents]
    if len(set(agent_ids)) != len(agent_ids):
        raise ScenarioError(f"{scenario.id}: duplicate agent ids")
    if len(agent_ids) < 2:
        raise ScenarioError(f"{scenario.id}: needs at least two agents")

    steps = [a.step for a in scenario.actions]
    if len(set(steps)) != len(steps):
        raise ScenarioError(f"{scenario.id}: steps must be unique")
    unknown = {a.agent for a in scenario.actions} - set(agent_ids)
    if unknown:
        raise ScenarioError(f"{scenario.id}: actions by unknown agents {sorted(unknown)}")
    idle = set(agent_ids) - {a.agent for a in scenario.actions}
    if idle:
        raise ScenarioError(f"{scenario.id}: agents with no actions {sorted(idle)}")

    if scenario.is_attack:
        if not scenario.combined_harm:
            raise ScenarioError(f"{scenario.id}: attack needs combined_harm")
        missing = set(scenario.sabotage_steps) - set(steps)
        if missing:
            raise ScenarioError(f"{scenario.id}: sabotage steps not in actions {sorted(missing)}")
        step_agent = {a.step: a.agent for a in scenario.actions}
        saboteurs = {step_agent[s] for s in scenario.sabotage_steps}
        # The point of a split attack: the harm needs more than one agent.
        if len(saboteurs) < 2:
            raise ScenarioError(f"{scenario.id}: sabotage must be split across agents")
    elif scenario.sabotage_steps or scenario.combined_harm:
        raise ScenarioError(f"{scenario.id}: benign scenario must not list sabotage")


def load_scenario(path: Path) -> Scenario:
    raw = json.loads(path.read_text())
    scenario = Scenario(
        id=raw["id"],
        kind=raw["kind"],
        project=raw["project"],
        reported_result=raw["reported_result"],
        agents=tuple(Agent(**a) for a in raw["agents"]),
        actions=tuple(Action(**a) for a in raw["actions"]),
        sabotage_steps=tuple(raw.get("sabotage_steps", ())),
        combined_harm=raw.get("combined_harm", ""),
    )
    validate(scenario)
    return scenario


def load_all(directory: Path = SCENARIOS_DIR) -> list[Scenario]:
    return [load_scenario(p) for p in sorted(directory.glob("*.json"))]
