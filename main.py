"""Backward-compatible ASGI entry point.

The canonical BarSan Experiment application lives in barsan_experiment.py.
Keep this module because existing cPanel configurations may still target main:app.
"""
from barsan_experiment import app  # noqa: F401
