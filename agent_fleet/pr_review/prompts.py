"""Prompt builder for two-pass PR analysis."""

from __future__ import annotations

import textwrap

from agent_fleet.pr_review.config import (
    DEFAULT_QUALITY_REVIEW_SKILLS,
    PrReviewConfig,
    load_overlay_text,
)
from agent_fleet.pr_review.git import classify_files, diff_for_files, truncate_diff
from agent_fleet.skills_lib import load_skill_text

JSON_OUTPUT_SPEC = textwrap.dedent("""\
    Return your complete assessment as **strict JSON**:
    {
      "pr_type": "frontend|backend|pipeline|ops|docs|mixed|other",
      "primary_areas": ["affected areas"],
      "risk_level": "low|medium|high|critical",
      "risk_reasoning": "specific technical justification",
      "summary": "1-2 sentences on what this PR does",
      "deep_analysis": "detailed technical analysis with file/line citations",
      "recommendations": {
        "frontend_check": true,
        "backend_check": true,
        "pipeline_check": true,
        "security_check": true,
        "qa_check": true,
        "performance_check": true,
        "data_check": true,
        "ops_check": true
      },
      "methodology_checklist": {
        "integration_tests_present": true,
        "integration_tests_detail": (
            "which files contain the integration tests, or why none are needed"
        ),
        "error_paths_tested": true,
        "error_paths_detail": "which tests cover error/failure paths",
        "cross_system_contracts_verified": true,
        "cross_system_detail": "which contracts were checked",
        "debug_code_removed": true,
        "debug_code_detail": "any console.log, TODO, debugger found",
        "type_checking_verified": true,
        "type_checking_detail": "whether pyright/tsc passes or why not applicable"
      },
      "findings": [
        {
          "severity": "critical|high|medium|low",
          "area": "security|performance|frontend|backend|pipeline|data|ops|breaking|tests",
          "message": "specific issue with file/line refs"
        }
      ],
      "suggestions": ["actionable improvements with file refs"]
    }

    Rules for findings:
    - Every real issue gets ONE finding with explicit severity and area.
    - Use "critical" for exploitable security holes or guaranteed production outages.
    - Use "high" for likely bugs, significant regressions, or auth bypasses.
    - Use "medium" for missing edge-case handling or maintainability issues.
    - Use "low" for style nits or theoretical concerns.
    - Keep each message to 1-2 sentences max. Cite file/line numbers.
""")


def _prospective_audits_block(domain: str) -> str:
    return textwrap.dedent(f"""\
        ## Prospective audits (mandatory — run all three)

        Reason about the diff statically; you have no browser or running app.

        ### A. Inversion of claims
        For every behavioral claim implied by the changed code, generate the
        inverse / negation / boundary / absence path and check the diff covers it.
        Cite at least one inverse path per major claim.

        ### B. First-principles invariants ({domain})
        Apply canonical invariants for this diff's domain as artifact lookups
        (grep patterns, cite lines) — not imagined runtime behavior.

        ### C. Negative-space scan
        Find asymmetric pairs the diff added one half of (writer without reader,
        env var without docs, new fetch without error path, etc.).
    """)


def build_prompt(
    diff: str,
    files: list[str],
    mode: str,
    config: PrReviewConfig,
    *,
    skill_dirs: list | None = None,
) -> str:
    """Build a mode-specific review prompt with optional repo overlay."""
    classified = classify_files(files, config.area_prefixes)
    frontend_files = classified["frontend"]
    backend_files = classified["backend"]
    other_files = classified["other"]

    frontend_diff = truncate_diff(diff_for_files(diff, frontend_files), 6000)
    backend_diff = truncate_diff(diff_for_files(diff, backend_files), 6000)
    full_diff = truncate_diff(diff, config.max_diff_chars)

    sections: list[str] = []

    if mode == "frontend" and frontend_files:
        fe_list = "\n".join(f"  {path}" for path in frontend_files[:30])
        if len(frontend_files) > 30:
            fe_list += f"\n  ... and {len(frontend_files) - 30} more"
        sections.append(
            textwrap.dedent(f"""\
            ## Frontend Analysis

            Files ({len(frontend_files)}):
            {fe_list}

            ```diff
            {frontend_diff}
            ```

            Tasks:
            - Check React/TypeScript correctness, hook deps, key props
            - Verify styling, responsive behavior, accessibility basics
            - Look for hydration mismatches and SSR issues
            - Flag silent error swallowing on fetch chains
            - Assess bundle impact from new dependencies

            {_prospective_audits_block("frontend")}
        """)
        )

    elif mode == "backend-security":
        if backend_files:
            be_list = "\n".join(f"  {path}" for path in backend_files[:30])
            if len(backend_files) > 30:
                be_list += f"\n  ... and {len(backend_files) - 30} more"
            sections.append(
                textwrap.dedent(f"""\
                ## Backend Analysis

                Files ({len(backend_files)}):
                {be_list}

                ```diff
                {backend_diff}
                ```

                Tasks:
                - Check API signatures, validation, and data flow
                - Look for missing auth checks and unsafe queries
                - Assess pipeline/ETL correctness if data paths changed
                - Check for race conditions and async misuse

                {_prospective_audits_block("backend / pipeline")}
            """)
            )

        other_list = "\n".join(f"  {path}" for path in other_files[:20]) or "  (see sections above)"
        sections.append(
            textwrap.dedent(f"""\
            ## Security & QA Audit

            Other changed files:
            {other_list}

            ```diff
            {full_diff}
            ```

            Tasks:
            - Trace user input paths for injection risks
            - Check secrets handling and auth middleware
            - Look for missing/broken tests related to changed code
            - Flag breaking API or schema changes

            ## Cross-cutting prospective audit (mandatory)
            Check FE/BE/ops seams when the full diff spans multiple areas:
            API field ↔ consumer, env var ↔ docs, schema ↔ tests, etc.
        """)
        )

    elif mode == "quality":
        full_diff = truncate_diff(diff, config.max_diff_chars)
        skill_names = config.quality_review_skills or DEFAULT_QUALITY_REVIEW_SKILLS
        skill_bodies: list[str] = []
        if config.quality_review_enabled and skill_dirs:
            for skill_name in skill_names:
                try:
                    body = load_skill_text(skill_name, skill_dirs)
                except FileNotFoundError:
                    continue
                if body:
                    skill_bodies.append(f"### Skill: {skill_name}\n\n{body}")
        quality_body = "\n\n".join(skill_bodies)
        sections.append(
            textwrap.dedent(f"""\
            ## Thermo-nuclear code quality review (mandatory)

            Apply the maintainability standards below to the **entire diff**.
            Flag structural regressions, file-size explosions (>1k lines),
            spaghetti branching, and missed code-judo simplifications.

            {quality_body or "(quality review skill not found — apply strict maintainability bar)"}

            Changed files ({len(files)}):
            {chr(10).join(f"  {path}" for path in files[:40])}

            ```diff
            {full_diff}
            ```

            Map quality findings to `findings` with area `backend` or `frontend`
            and severity medium+ when they should block merge.
        """)
        )

    if not sections:
        sections.append("No relevant files for this analysis mode.")

    overlay = load_overlay_text(config)
    overlay_block = ""
    if overlay:
        overlay_block = textwrap.dedent(f"""\
            ## Repository-specific invariants (mandatory)

            {overlay}
        """)

    sections_body = "\n\n".join(sections)

    return textwrap.dedent(f"""\
        You are a principal engineer performing a PR review.
        Do NOT modify files. Do NOT run destructive commands.
        You MAY use Read and search tools to investigate context.
        Be specific. Cite file names and line numbers.
        Focus ONLY on code changed in this diff.

        Each section below includes mandatory prospective audits (Inversion /
        First-principles / Negative-space). Surface results in `deep_analysis`
        and `findings`.

        {overlay_block}

        {sections_body}

        {JSON_OUTPUT_SPEC}
    """)
