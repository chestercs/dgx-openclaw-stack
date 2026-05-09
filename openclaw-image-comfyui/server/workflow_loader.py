"""Workflow JSON template loader + parameter substitution.

Each workflow file under workflows/ is a literal ComfyUI API-format export
(a flat dict keyed by string node ids, where each value has `class_type`
and `inputs`) plus an extra `_metadata` block at the top that the bridge
strips before submission. The metadata declares:

  - name, description (human-readable; surfaced via list_workflows)
  - checkpoint_required (bool — true means the bridge MUST receive a
    `checkpoint=` arg or have the workflow pre-edited; false means the
    workflow's baked-in CheckpointLoaderSimple value is acceptable)
  - defaults (dict of optional param defaults)
  - targets (dict mapping bridge param names → {node, input}) — the
    explicit, robust way for a workflow to declare which knobs are
    tunable and where they live. For prompt/negative this is required
    (positive vs negative CLIPTextEncode otherwise ambiguous). For other
    knobs the bridge falls back to class_type lookup if `targets` is
    silent.

Substitution is by node-id + input-key, NEVER by string-replace on the
serialized JSON. String-replace would corrupt prompts that legitimately
contain `${…}` patterns (LoRA syntax, negative prompts, etc.).
"""
from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("workflow-loader")


# class_type → default `inputs` field name we should write our value into.
# Used as a fallback when a workflow's _metadata.targets is silent on a
# given parameter. Prompt/negative are deliberately absent (ambiguous).
CLASS_TYPE_FALLBACK = {
    "checkpoint":  ("CheckpointLoaderSimple", "ckpt_name"),
    "width":       ("EmptyLatentImage",       "width"),
    "height":      ("EmptyLatentImage",       "height"),
    "batch_size":  ("EmptyLatentImage",       "batch_size"),
    "seed":        ("KSampler",               "seed"),
    "steps":       ("KSampler",               "steps"),
    "cfg":         ("KSampler",               "cfg"),
    "sampler":     ("KSampler",               "sampler_name"),
    "scheduler":   ("KSampler",               "scheduler"),
}


class WorkflowError(ValueError):
    """Raised when a workflow can't be bound (missing target, etc)."""


class Workflow:
    def __init__(self, name: str, raw: dict) -> None:
        self.name = name
        meta = dict(raw.get("_metadata") or {})
        # The graph is everything except _metadata. Keep a deep copy so
        # `bind()` can mutate without corrupting the cached template.
        graph = {k: v for k, v in raw.items() if k != "_metadata"}
        self._graph_template = graph
        self.description: str = meta.get("description") or ""
        self.checkpoint_required: bool = bool(meta.get("checkpoint_required", False))
        self.defaults: dict = dict(meta.get("defaults") or {})
        self.targets: dict = dict(meta.get("targets") or {})

    def declared_overrides(self) -> list[str]:
        # Surface to the agent which params this workflow accepts. Useful
        # in `comfyui_image__list_workflows` so the agent doesn't have to
        # guess.
        keys = set(self.targets.keys()) | set(self.defaults.keys())
        # Prompt/seed are always supported even if the workflow doesn't
        # mention them explicitly — bind() requires `prompt`.
        keys.add("prompt")
        keys.add("seed")
        return sorted(keys)

    def _find_node_by_class_type(self, class_type: str) -> Optional[str]:
        """Return the node id of the first node whose class_type matches.
        Used by class_type fallback when _metadata.targets is silent."""
        for node_id, node in self._graph_template.items():
            if isinstance(node, dict) and node.get("class_type") == class_type:
                return node_id
        return None

    def _resolve_target(self, param: str) -> Optional[list[tuple[str, str]]]:
        """Return list of (node_id, input_key) bindings for `param`, or
        None if unmappable. Priority: explicit _metadata.targets →
        CLASS_TYPE_FALLBACK. Always returns a list (single-element for
        legacy dict form, multi-element for array form).

        Three target shapes supported in `_metadata.targets.<param>`:

          1. Single dict   `{"node": "20", "input": "width"}` →
             one binding, single-element list.
          2. Array of dicts `[{"node":"20","input":"width"},
                              {"node":"21","input":"width"}]` →
             multiple bindings, override applies to ALL listed nodes.
             Used when a single bridge knob (e.g. width) needs to
             stay in lockstep across two graph nodes — e.g.
             EmptyLatentImage + ModelSamplingFlux for FLUX where
             both expect the same resolution for proper sigma
             scheduling.
          3. Literal `false` → opt-out from class_type fallback.
             Used by multi-stage workflows that have multiple nodes
             of the fallback's class_type (e.g. SUPIR's Juggernaut
             CheckpointLoaderSimple) where the bridge's first-match
             fallback would corrupt the wrong slot."""
        explicit = self.targets.get(param)
        if explicit is False:
            return None
        if isinstance(explicit, list):
            bindings: list[tuple[str, str]] = []
            for item in explicit:
                if isinstance(item, dict):
                    node = item.get("node")
                    inp = item.get("input")
                    if isinstance(node, str) and isinstance(inp, str):
                        bindings.append((node, inp))
            return bindings if bindings else None
        if isinstance(explicit, dict):
            node = explicit.get("node")
            inp = explicit.get("input")
            if isinstance(node, str) and isinstance(inp, str):
                return [(node, inp)]
        fallback = CLASS_TYPE_FALLBACK.get(param)
        if fallback is None:
            return None
        class_type, input_key = fallback
        node_id = self._find_node_by_class_type(class_type)
        if node_id is None:
            return None
        return [(node_id, input_key)]

    def bind(self, args: dict[str, Any]) -> dict:
        """Produce a ComfyUI-ready prompt dict by applying `args` to the
        template. Returns a fresh dict — the loader's cached template is
        never mutated. Raises WorkflowError on missing required targets
        or unfillable checkpoint placeholders."""
        graph = copy.deepcopy(self._graph_template)

        # Prompt is mandatory and prompt has no class_type fallback (we
        # cannot guess which CLIPTextEncode is positive). The workflow
        # MUST declare `targets.prompt` — every shipped workflow does.
        prompt_target = self.targets.get("prompt")
        if not isinstance(prompt_target, dict):
            raise WorkflowError(
                f"workflow {self.name!r} does not declare _metadata.targets.prompt — "
                "every workflow must mark which CLIPTextEncode receives the user prompt"
            )
        prompt_text = args.get("prompt")
        if not isinstance(prompt_text, str) or not prompt_text:
            raise WorkflowError("`prompt` is required and must be a non-empty string")
        _set_input(graph, prompt_target.get("node"), prompt_target.get("input"), prompt_text)

        # Optional params — only write if the caller provided a value AND
        # the workflow has a target (explicit or fallback). Silently skip
        # otherwise; the workflow's baked-in default applies.
        for key in (
            "negative", "checkpoint", "width", "height", "batch_size",
            "seed", "steps", "cfg", "sampler", "scheduler",
        ):
            value = args.get(key)
            if value is None:
                continue
            target = (
                self._resolve_target(key)
                if key != "negative"
                else _negative_target(self.targets)
            )
            if target is None:
                # Caller asked to override `key` but workflow exposes no
                # such knob. That's a soft mismatch — surface to the log
                # but don't fail (the user may be passing args that fit
                # a different workflow).
                log.info("workflow %r ignored override for %r (no target)", self.name, key)
                continue
            # `target` is now a list of (node_id, input_key) — single
            # entry for the legacy dict form, multi-entry for the array
            # form (used when one bridge knob has to stay in lockstep
            # across two graph nodes — e.g. width/height on
            # EmptyLatentImage AND ModelSamplingFlux for FLUX).
            for node_id, input_key in target:
                _set_input(graph, node_id, input_key, value)

        # Checkpoint required-but-not-set guard. If the workflow declares
        # checkpoint_required and the resulting graph still has the
        # REPLACE_ME placeholder, refuse. The list form is iterated;
        # if ANY of the targets still has a placeholder, the guard fires.
        if self.checkpoint_required:
            ck_targets = self._resolve_target("checkpoint")
            if ck_targets is not None:
                for node_id, input_key in ck_targets:
                    current = graph.get(node_id, {}).get("inputs", {}).get(input_key)
                    if isinstance(current, str) and current.startswith("REPLACE_ME"):
                        raise WorkflowError(
                            f"workflow {self.name!r} requires a checkpoint name. Pass "
                            "`checkpoint=...` to comfyui_image__generate, or edit the "
                            f"workflow JSON to replace the REPLACE_ME placeholder. "
                            "Models live under ComfyUI's basedir/models/checkpoints/."
                        )

        return graph


def _negative_target(targets: dict) -> Optional[list[tuple[str, str]]]:
    """Negative prompt has no class_type fallback (multiple
    CLIPTextEncode nodes are normal). Workflows must declare it
    explicitly under targets.negative; otherwise the negative override
    is silently dropped. Same array-or-dict shape semantics as
    `_resolve_target` — single dict or list of dicts both supported."""
    explicit = targets.get("negative")
    if isinstance(explicit, list):
        bindings: list[tuple[str, str]] = []
        for item in explicit:
            if isinstance(item, dict):
                node = item.get("node")
                inp = item.get("input")
                if isinstance(node, str) and isinstance(inp, str):
                    bindings.append((node, inp))
        return bindings if bindings else None
    if isinstance(explicit, dict):
        node = explicit.get("node")
        inp = explicit.get("input")
        if isinstance(node, str) and isinstance(inp, str):
            return [(node, inp)]
    return None


def _set_input(graph: dict, node_id: Optional[str], input_key: Optional[str], value: Any) -> None:
    if not isinstance(node_id, str) or not isinstance(input_key, str):
        return
    node = graph.get(node_id)
    if not isinstance(node, dict):
        raise WorkflowError(f"workflow refers to missing node id {node_id!r}")
    inputs = node.setdefault("inputs", {})
    inputs[input_key] = value


class WorkflowLoader:
    """Loads every `*.json` under a directory at startup. Skips files
    whose top-level structure isn't a dict (probably a malformed export)
    and `*.json.example` placeholders shipped for operator templates."""

    def __init__(self, workflow_dir: str | Path) -> None:
        self.workflow_dir = Path(workflow_dir)
        self._cache: dict[str, Workflow] = {}

    def load_all(self) -> dict[str, Workflow]:
        if not self.workflow_dir.is_dir():
            log.warning("workflow dir %s missing", self.workflow_dir)
            return {}
        loaded: dict[str, Workflow] = {}
        for path in sorted(self.workflow_dir.glob("*.json")):
            if path.name.endswith(".example"):
                continue
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except Exception as e:  # noqa: BLE001
                log.warning("skipping %s (parse error: %s)", path.name, e)
                continue
            if not isinstance(raw, dict):
                log.warning("skipping %s (top-level is not an object)", path.name)
                continue
            name = path.stem
            try:
                loaded[name] = Workflow(name=name, raw=raw)
            except Exception as e:  # noqa: BLE001
                log.warning("skipping %s (workflow init failed: %s)", path.name, e)
        self._cache = loaded
        log.info("loaded %d workflows: %s", len(loaded), sorted(loaded.keys()))
        return dict(loaded)

    def get(self, name: str) -> Workflow:
        wf = self._cache.get(name)
        if wf is None:
            raise WorkflowError(
                f"unknown workflow {name!r}. Available: {sorted(self._cache.keys())}"
            )
        return wf

    def list(self) -> list[dict]:
        return [
            {
                "name": wf.name,
                "description": wf.description,
                "checkpoint_required": wf.checkpoint_required,
                "defaults": wf.defaults,
                "declared_overrides": wf.declared_overrides(),
            }
            for wf in self._cache.values()
        ]
