# Agent Fleet

A local swarm of scoped coding agents dispatched at git repos via a single `fleet` CLI, Python API, or GitHub issue comments.

## Language

**Fleet**:
The globally installed `fleet` CLI and its backing dispatcher — the top-level concept encompassing all agents, personas, pipelines, and configuration. It is installed once per machine and pointed at any git workspace.
_Avoid_: agent-fleet (tool name only), swarm

**Agent**:
A single Cursor Composer session scoped to a task and a set of allowed paths. Each agent runs one pipeline and produces a structured result.
_Avoid_: bot, worker, job

**Persona**:
A named role definition — a markdown prompt plus optional model and path allowlists — that shapes an agent's behavior for a class of task (e.g. `coder`, `reviewer`, `pr-analyzer`). Personas are declared in `fleet.yaml` and optionally overridden per repo.
_Avoid_: role, profile, template

**Summon**:
The idempotent first-run command (`fleet summon`) that scaffolds repo config if absent, runs environment checks, and prints a ready banner. Safe to run repeatedly.
_Avoid_: init, bootstrap, setup

**Run**:
A single dispatched task: one goal sent through one pipeline to one agent, producing a JSON result and a JSONL log entry.
_Avoid_: job, execution, invocation

**Review**:
A two-pass Composer PR analysis (`fleet review`) that reads the diff between the workspace and a base branch and returns a structured verdict (`approve` / `request_changes` / `block`).
_Avoid_: PR check, lint

**Scope**:
The ranked list of fleet-dispatchable tasks derived from open GitHub issues via quality analysis (`fleet scope`). Also the per-persona path allowlist in `.agent-fleet.yaml` that constrains which files an agent may touch.
_Avoid_: backlog, triage (for the command); allowlist (for the path constraint — use "scope allowlist")

**Scout**:
A read-only intake pass (`fleet scout`) that surveys open issues and the product context to classify, prioritize, and surface engineering or product signals without dispatching any coding agents.
_Avoid_: triage, analysis pass

**Loop**:
The continuous PR watcher (`fleet loop`) that polls open `fleet/*` branches, dispatches fix agents when review findings exist, waits for CI, and optionally merges.
_Avoid_: watcher, daemon, PR loop

**Dispatch**:
The act of sending one or more tasks to the fleet, either from the CLI (`fleet run`), issue-comment trigger (`fleet dispatch`), or the Python `dispatch_tasks()` API.
_Avoid_: trigger, submit, enqueue

**Watch**:
The live run viewer (`fleet watch`) that tails a single run's phase and agent tree as it executes, identified by run id, a unique prefix, or `latest`.
_Avoid_: tail, monitor, follow

**Workstream**:
A named batch of fleet tasks declared in `.agent-fleet.yaml` under `workstreams:`, run together as a coordinated unit via `fleet workstream run`. Each item in a workstream is one goal/persona pair.
_Avoid_: batch, task group, suite

**DAG**:
A dependency-graph task file (JSON) where nodes are agent tasks and edges declare upstream/downstream ordering. `fleet dag run` executes ranks in parallel waves, stitching upstream outputs into downstream prompts.
_Avoid_: pipeline graph, task graph, workflow

**Backend**:
The agent execution service (e.g. `cursor`, `kimi`) configured in `fleet.yaml` via `default_backend`. The backend provides the Composer API that agents call.
_Avoid_: provider, engine, service
