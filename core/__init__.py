"""Core engine (matcher, signals, realizable-edge, storage).

In production this package holds the real intelligence engine. During this
iteration it is backed by ``core.mock`` with realistic, same-signature stubs.
The MCP server calls into here and must NOT duplicate this logic.
"""
