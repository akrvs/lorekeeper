"""Maintenance agents. Importing the package registers every built-in agent."""

from app.agents import dedup, staleness  # noqa: F401  (register the agents)
from app.agents.base import AgentFactory, MaintenanceAgent

__all__ = ["AgentFactory", "MaintenanceAgent"]
