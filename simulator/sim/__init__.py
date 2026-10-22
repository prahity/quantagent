"""Independent market simulator and evaluator.

Boundary rule: nothing under ``sim`` may import ``agent``. The agent only talks to the
simulator through the versioned JSON contract in ``contracts``.
"""
