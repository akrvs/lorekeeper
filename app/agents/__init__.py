"""Maintenance agents. Importing the package registers every built-in agent."""

from app.agents import (  # noqa: F401  (register the agents)
    contradiction,
    dedup,
    edge_suggest,
    hygiene,
    staleness,
)
from app.agents.base import AgentFactory, MaintenanceAgent

__all__ = ["AgentFactory", "MaintenanceAgent"]
