"""Regression-grade fixtures shared by gates and CI baselines.

Modules under this package are *not* unit tests — they are deterministic
producers consumed by ``scripts/check_*.py`` gates that compare runtime
output against a committed baseline.
"""
