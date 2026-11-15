"""Attest client: ask the gate whether an agent can be trusted, from Python, the shell or MCP."""
from .client import Attest, Decision, build_request

__all__ = ["Attest", "Decision", "build_request"]
