# Scheduled Fleet Dispatch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add declarative cron schedules to `.agent-fleet.yaml` so fleet jobs (e.g. daily docs sync) fire automatically from the existing watcher daemon.

**Architecture:** New `agent_fleet.schedule` package evaluates cron expressions on each watcher poll, stores per-job state in unified `.agent-fleet-state.json`, and spawns existing issue dispatch or a new lightweight task dispatch subprocess. `CombinedWatcher` gains a third poll leg.

**Tech Stack:** Python 3.14, croniter, zoneinfo, existing FleetCapacityGate + in_flight tracking.

---

### Task 1: Config + cron helpers

**Files:** `agent_fleet/schedule/config.py`, `agent_fleet/schedule/cron.py`, `agent_fleet/repo.py`

### Task 2: Dispatch spawns + task runner

**Files:** `agent_fleet/schedule/dispatch.py`, `agent_fleet/schedule/task_dispatch.py`, `agent_fleet/in_flight.py`

### Task 3: ScheduleWatcher + CombinedWatcher integration

**Files:** `agent_fleet/schedule/watcher.py`, `agent_fleet/issue_loop/watcher.py`, `agent_fleet/state.py`

### Task 4: CLI + docs + tests

**Files:** `agent_fleet/schedule/cli.py`, `agent_fleet/cli.py`, `pyproject.toml`, `tests/test_schedule.py`, `docs/SCHEDULES.md`, `examples/repo.agent-fleet.yaml`
