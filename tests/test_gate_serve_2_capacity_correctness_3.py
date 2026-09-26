"""correctness-3: ``restore`` coerces target fields with a bare ``int()``.

``CapacityController.restore`` is the first thing a restarting supervisor calls
with the on-disk ``capacity.json``. The scalar fields it does not care about are
defensively guarded — ``idle_ticks`` is behind ``isinstance(idle, int)`` and
``saturated`` behind ``isinstance(saturated, bool)`` — but the four target
fields go straight through ``int(...)`` with no type check and no try/except.

So a non-integer in ``targets.max_lanes`` raises ``ValueError`` and takes
supervisor startup down. That is precisely the case the surrounding isinstance
checks and the docstring's tolerance for a partial payload exist to absorb: a
hand-edited file, a half-written file, a value written by a different version
of the schema, or a typo in ``lanes_ceiling`` when it was serialised.

The failure is asymmetric and worth stating: a garbage ``idle_ticks`` is
silently ignored, while garbage ``max_lanes`` is fatal. Both come from the same
truncated file.
"""

from __future__ import annotations

from agent_fleet.serve.capacity import CapacityController


def test_restore_ignores_a_non_integer_target_field() -> None:
    controller = CapacityController()
    before = controller.targets.max_lanes

    controller.restore({"targets": {"max_lanes": "abc"}})

    assert controller.targets.max_lanes == before, (
        "a non-integer max_lanes should be ignored exactly like a non-integer "
        "idle_ticks two lines below, not raise and abort supervisor startup"
    )


def test_restore_tolerates_a_partially_corrupt_targets_block() -> None:
    """A truncated hand-edit usually breaks more than one field at once."""
    controller = CapacityController()
    before = controller.targets

    controller.restore(
        {
            "targets": {
                "max_lanes": "4",
                "max_gates": None,
                "test_pool": [1, 2],
                "typecheck_pool": {"n": 3},
                "signal": 7,
            }
        }
    )

    assert isinstance(controller.targets.max_lanes, int)
    assert isinstance(controller.targets.max_gates, int)
    assert isinstance(controller.targets.test_pool, int)
    assert isinstance(controller.targets.typecheck_pool, int)
    assert controller.targets.max_lanes == before.max_lanes
