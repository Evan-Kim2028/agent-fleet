"""Model policy for the lane manager.

The owner's policy is narrow and deliberate, and this module is the only place
that encodes it:

* ``cmd`` — the implementation engine — may run **only**
  ``stealth/space-bunny-alpha``.
* ``grok`` is permitted **only** as a gate judge, and only on
  ``step-5-preview``.

Rather than leaving that to operator discipline (the bash drivers enforced it
only by `unset FB_MODEL` and a hardcoded ``-m`` flag), the lane manager
*validates* every model it is about to use. An out-of-policy model raises
``ModelPolicyError`` before any subprocess is spawned, so a stray
``AGENT_FLEET_MODEL`` or ``FB_MODEL`` in the environment cannot redirect a
lane to a different model family.
"""

from __future__ import annotations

#: The one model the ``cmd`` engine may use.
CMD_MODEL = "stealth/space-bunny-alpha"

#: The one model a grok gate judge may use.
GROK_JUDGE_MODEL = "step-5-preview"

#: Engines that implement a task.
IMPLEMENTATION_ENGINES = ("cmd", "devin")

#: Models permitted for an implementation run, keyed by engine.
ENGINE_MODELS: dict[str, str] = {
    "cmd": CMD_MODEL,
    # Devin keeps its own two-step ladder: `ensure_pr` walks down this list on
    # a capacity error (ported from devin_finish.sh).
    "devin": "swe-2-high",
}

#: The devin model ladder, walked in order on a capacity error.
DEVIN_MODEL_LADDER: tuple[str, ...] = ("swe-2-high", "swe-2-medium")

#: Purpose for which a model is being resolved. Only ``judge`` may use grok.
MODEL_ROLES = ("implement", "judge")


class ModelPolicyError(ValueError):
    """Raised when a requested model is outside the lane manager's policy."""


def resolve_engine_model(engine: str, *, role: str = "implement") -> str:
    """Return the policy model for *engine*, or raise ``ModelPolicyError``.

    *role* is ``"implement"`` for the implementer run and ``"judge"`` for the
    gate. A ``judge`` on the ``grok`` engine resolves to
    ``step-5-preview``; a ``grok`` engine in the ``implement`` role is
    rejected outright — grok may judge, never implement.
    """
    engine = (engine or "").strip().lower()
    if role not in MODEL_ROLES:
        raise ModelPolicyError(f"unknown model role {role!r}; expected one of {MODEL_ROLES}")

    if engine == "grok":
        if role != "judge":
            raise ModelPolicyError(
                "grok may only be used as a gate judge (role='judge' on "
                "step-5-preview); it is not permitted for implementation"
            )
        return GROK_JUDGE_MODEL

    if role != "implement":
        raise ModelPolicyError(f"role {role!r} is not valid for engine {engine!r}")

    try:
        return ENGINE_MODELS[engine]
    except KeyError:
        raise ModelPolicyError(
            f"unknown engine {engine!r}; expected one of {sorted(ENGINE_MODELS)}"
        ) from None


def enforce_implementation_model(engine: str, model: str | None) -> str:
    """Return *model* if it satisfies the policy for *engine*, else raise.

    Unlike ``resolve_engine_model`` this honours an explicitly requested model
    (from config or the environment) — but only to *validate* it. A request for
    a different model is an error, not a silent override, so the guarantee that
    every lane runs on the policy model is checkable at runtime.
    """
    expected = resolve_engine_model(engine)
    if model is None or not str(model).strip():
        return expected
    requested = str(model).strip()
    if requested != expected:
        raise ModelPolicyError(
            f"engine {engine!r} is pinned to {expected!r} by the lane model policy; "
            f"refusing to run {requested!r}"
        )
    return requested
