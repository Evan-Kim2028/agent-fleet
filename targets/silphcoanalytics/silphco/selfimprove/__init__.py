"""silphco.selfimprove — nightly self-improvement loop for the Agent Fleet.

Public surface:
    mine.py     — deterministic failure-signature mining (no LLM)
    propose.py  — single-call LLM proposer; returns ChangeProposal
    guard.py    — path allowlist/denylist enforcement
    gate.py     — promptfoo regression gate
    loop.py     — orchestrator: mine → propose → guard → gate → PR
    __main__.py — console entry: ``python -m silphco.selfimprove``
"""
