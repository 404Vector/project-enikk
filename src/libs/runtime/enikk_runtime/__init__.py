"""Engine-neutral runtime framework shared by every engine runtime (ADR-0010).

Leases tasks from enikk-core, resolves the prompt, runs a ``ModelEngine`` while
keeping the lease alive, and reports the outcome. Knows no model provider SDK (I-08).
"""
