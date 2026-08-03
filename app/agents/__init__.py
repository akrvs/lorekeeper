"""Maintenance agents. Importing the package registers every built-in agent."""

from app.agents import contradiction, dedup, hygiene, staleness  # noqa: F401  (register)
from app.agents.base import AgentFactory, MaintenanceAgent

__all__ = ["AgentFactory", "MaintenanceAgent"]
