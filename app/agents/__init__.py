"""Maintenance agents. Importing the package registers every built-in agent."""

from app.agents import dedup  # noqa: F401  (registers "dedup")
from app.agents.base import AgentFactory, MaintenanceAgent

__all__ = ["AgentFactory", "MaintenanceAgent"]
