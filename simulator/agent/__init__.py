"""Test agents that speak the simulator's JSON contract.

Boundary rule: nothing under ``agent`` may import ``sim``. Agents receive decision
requests and a feed reader (the released data interface) and return action JSON.
"""
