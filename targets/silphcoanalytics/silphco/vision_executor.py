"""VisionExecutor — AgentExecutor for the DESIGN_REVIEW phase.

Satisfies ``fleet.hooks.AgentExecutor`` by structural subtyping.

Architecture
------------
This module ships in two parts:

1. **StubVisionExecutor** (default, always importable):
   Returns a schema-valid neutral ``DesignReview`` pass without calling any
   external API.  Used when no real vision backend is configured so the
   pipeline never hard-fails unconfigured.

2. **Extension point** (documented below):
   Drop-in replacement that calls a real multimodal backend (Claude Vision
   API, zai-mcp vision, etc.).  See ``_RealVisionBackend`` for the
   documented wiring pattern.

Real-vision extension point
---------------------------
To wire a real multimodal backend:

1. Subclass ``VisionExecutor`` and override ``_call_vision_backend``:

   .. code-block:: python

       class ClaudeVisionExecutor(VisionExecutor):
           def __init__(self, api_key: str, model: str = "claude-opus-4-5"):
               super().__init__()
               self._client = anthropic.Anthropic(api_key=api_key)
               self._model = model

           def _call_vision_backend(
               self, prompt: str, image_paths: list[Path]
           ) -> str:
               # Build content blocks: text + base64-encoded images
               content = [{"type": "text", "text": prompt}]
               for path in image_paths:
                   data = base64.standard_b64encode(path.read_bytes()).decode()
                   content.append({
                       "type": "image",
                       "source": {
                           "type": "base64",
                           "media_type": "image/png",
                           "data": data,
                       },
                   })
               msg = self._client.messages.create(
                   model=self._model,
                   max_tokens=2048,
                   messages=[{"role": "user", "content": content}],
               )
               return msg.content[0].text

2. Or use ``zai-mcp-server`` (MCP tool) in a Claude Code context:

   .. code-block:: python

       class ZaiVisionExecutor(VisionExecutor):
           def _call_vision_backend(self, prompt: str, image_paths: list[Path]) -> str:
               # Calls mcp__zai-mcp-server__analyze_image for each screenshot
               # and concatenates the results into a DesignReview JSON.
               # Implementation is environment-specific.
               ...

3. Register the executor in your agent-fleet integration layer (see ``.agent-fleet.yaml`` and repo adapters under ``agents/silphco/``)::

       if spine.design_review_enabled:
           vision_exc = ClaudeVisionExecutor(api_key=os.environ["ANTHROPIC_API_KEY"])
           # pass vision_exc to build_runner(...)

The executor key (``spine.design_executor_key``, default ``"vision"``) allows
the entrypoint to look up the right executor per phase without hardcoding.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from agent_fleet.hooks import ExecutorResult

log = logging.getLogger(__name__)

# Schema-valid neutral DesignReview JSON that the stub always returns.
_NEUTRAL_PASS_JSON: dict[str, Any] = {
    "scores": {},
    "issues": [],
    "verdict": "pass",
}


class VisionExecutor:
    """AgentExecutor for the DESIGN_REVIEW phase.

    Default behaviour: stub that returns a neutral pass without calling any
    external API.  Override ``_call_vision_backend`` to wire a real model.

    This class is intentionally not abstract — it works safely as-is when no
    vision backend is configured, so the pipeline never hard-fails during
    development or in environments without vision credentials.
    """

    def execute(
        self,
        phase_name: str,
        prompt: str,
        *,
        context: dict[str, Any] | None = None,
        attachments: Sequence[Path] = (),
    ) -> ExecutorResult:
        """Execute a DESIGN_REVIEW prompt, optionally with image attachments.

        When ``attachments`` is empty (no captured screenshots), immediately
        returns a neutral pass — no backend call is made.

        When ``attachments`` is non-empty, calls ``_call_vision_backend``
        which by default returns the neutral pass JSON string.  Override
        ``_call_vision_backend`` in a subclass to route to a real model.

        Args:
            phase_name:  Expected to be ``"DESIGN_REVIEW"``; logged if not.
            prompt:      Critic prompt from ``design_review._build_prompt``.
            context:     Optional executor hints (ignored by stub).
            attachments: Image paths from ``CaptureArtifact.image_path``.

        Returns:
            ``ExecutorResult`` with ``stdout`` containing a schema-valid
            ``DesignReview`` JSON string.
        """
        if phase_name != "DESIGN_REVIEW":
            log.warning(
                "VisionExecutor received unexpected phase_name=%r (expected DESIGN_REVIEW)",
                phase_name,
            )

        if not attachments:
            log.debug("VisionExecutor: no attachments — returning neutral pass")
            return ExecutorResult(
                stdout=json.dumps(_NEUTRAL_PASS_JSON),
                stderr="",
                exit_code=0,
                duration_s=0.0,
            )

        try:
            stdout = self._call_vision_backend(prompt, list(attachments))
            return ExecutorResult(stdout=stdout, stderr="", exit_code=0, duration_s=0.0)
        except Exception as exc:
            log.error("VisionExecutor._call_vision_backend raised: %s", exc)
            # Return neutral pass so the pipeline never hard-crashes on a
            # vision backend error.  The design_review phase will log the
            # failure and return a neutral pass result.
            return ExecutorResult(
                stdout=json.dumps(_NEUTRAL_PASS_JSON),
                stderr=str(exc),
                exit_code=1,
                duration_s=0.0,
            )

    def _call_vision_backend(self, prompt: str, image_paths: list[Path]) -> str:
        """Call the multimodal backend with *prompt* and *image_paths*.

        **Default implementation: stub — returns neutral pass JSON.**

        Override this method in a subclass to call a real vision model.
        The returned string must be a valid ``DesignReview`` JSON object
        (see ``fleet/schemas/design_review.schema.json``).

        Extension point contract:
        - Return a JSON string parseable by ``DesignReview.from_dict``.
        - Raise any exception on hard failure; ``execute()`` will catch it
          and return a neutral pass to avoid hard-blocking the pipeline.
        - Do NOT hard-code a model name here; accept it as a constructor
          parameter so the caller controls the model/vendor.

        Args:
            prompt:      Critic prompt text (from ``design_review._build_prompt``).
            image_paths: Absolute paths to PNG screenshots.  Each path is
                         guaranteed to exist when this method is called.

        Returns:
            JSON string containing a valid DesignReview object.
        """
        log.debug(
            "VisionExecutor._call_vision_backend stub: %d image(s) — returning neutral pass. "
            "Override this method to call a real vision model.",
            len(image_paths),
        )
        return json.dumps(_NEUTRAL_PASS_JSON)


# ---------------------------------------------------------------------------
# Convenience alias: StubVisionExecutor = VisionExecutor (the base IS the stub)
# ---------------------------------------------------------------------------

StubVisionExecutor = VisionExecutor
