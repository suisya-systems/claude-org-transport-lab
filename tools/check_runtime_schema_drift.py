#!/usr/bin/env python3
"""Drift check: ja's ``tools/org_extension_schema.json`` vs the
``role_configs_schema.json`` bundled inside the ``claude-org-runtime``
package (Phase 5c, Issue #130).

The runtime ships its own copy of the role-configs schema (used by
``claude_org_runtime.settings.generator``). Historically the two copies
have been kept in lockstep by hand. This tool fails CI when they
diverge so the next contributor doesn't unknowingly publish a
runtime-incompatible schema change.

Pin-window tolerance
--------------------
ja pins ``claude-org-runtime`` to a narrow window (see
``RUNTIME_PIN_LOWER_INCLUSIVE`` / ``RUNTIME_PIN_UPPER_EXCLUSIVE``).
When the installed runtime is *outside* that window, the **byte check
skips with a warning** rather than failing — the bundled schema is by
definition allowed to evolve ahead of ja's pin window, and a hard
failure here would just block unrelated PRs. Inside the window the
byte check treats any structural difference as a hard failure.

The ``--semantic`` check, in contrast, runs unconditionally when
explicitly requested: an operator invoking it against a preview
runtime is asking "does my evaluator-shape golden still match?", and
silently skipping would defeat the point of the flag. The same
unconditional semantics apply to the matching pytest test.

Two drift dimensions
--------------------
The byte-identical schema check is necessary but not sufficient. Two
schemas can be byte-identical and still produce divergent rendered
sandbox suppression metadata if the runtime's
``render_role_with_metadata()`` evaluator changes shape (e.g. a new
suppression reason wording, a new placeholder substituted, a different
``sandbox_read_roots`` order). The "byte-identical" framing alone is
therefore incomplete after the Phase 1 sandbox-intent work landed.

The ``--semantic`` flag adds a complementary semantic golden drift
check: small in-repo fixtures under
``tests/fixtures/runtime_schema_drift/sandbox_intent/`` carry both an
input schema fragment and the expected explain JSON
(``SandboxMetadata.to_jsonable()``). The check renders each fixture
through ``render_role_with_metadata()`` with a fixture-supplied fake
``realpath`` shim (so platform-dependent paths don't pollute the
diff) and asserts the rendered explain JSON matches byte-for-byte.

Exit codes: 0 = OK or skipped (out-of-window), 1 = drift detected.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import sys
from importlib.metadata import PackageNotFoundError, version as _pkg_version
from pathlib import Path
from typing import Any, Callable

# Mirror of the ``claude-org-runtime`` pin in pyproject.toml /
# requirements.txt. Keep this constant in sync when widening the pin
# (Phase 5e+ scope per CLAUDE.local.md). Stored as tuples for ordered
# comparison; only major.minor.micro are honoured.
RUNTIME_PIN_LOWER_INCLUSIVE = (0, 1, 30)
RUNTIME_PIN_UPPER_EXCLUSIVE = (0, 2, 0)

REPO_ROOT = Path(__file__).resolve().parent.parent
JA_SCHEMA = REPO_ROOT / "tools" / "org_extension_schema.json"
SEMANTIC_FIXTURE_DIR = (
    REPO_ROOT / "tests" / "fixtures" / "runtime_schema_drift" / "sandbox_intent"
)


def _parse_version(ver: str) -> tuple[int, ...]:
    """Parse a PEP 440 release segment to a (major, minor, micro) tuple.

    Pre-release / dev / local segments are stripped — drift tolerance
    is decided on the release segment alone, matching how the pin
    specifier ``>=0.1.1,<0.2`` is interpreted by pip.
    """
    head = ver.split("+", 1)[0]
    for marker in ("a", "b", "rc", ".dev", ".post"):
        idx = head.find(marker)
        if idx != -1:
            head = head[:idx]
    parts: list[int] = []
    for chunk in head.split("."):
        if not chunk.isdigit():
            break
        parts.append(int(chunk))
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def _runtime_in_pin_window(installed: tuple[int, ...]) -> bool:
    return RUNTIME_PIN_LOWER_INCLUSIVE <= installed < RUNTIME_PIN_UPPER_EXCLUSIVE


def _bundled_schema_path() -> Path:
    """Return the on-disk path to the runtime's bundled
    ``role_configs_schema.json``.

    Imports lazily so the script can fail with a helpful message when
    ``claude-org-runtime`` is not installed, rather than dying at
    import time.
    """
    from importlib.resources import files

    resource = files("claude_org_runtime.settings").joinpath(
        "role_configs_schema.json"
    )
    return Path(str(resource))


def _normalise(obj: object) -> object:
    """Strip cosmetic-only keys before comparison.

    ``$comment`` entries are documentation aids and may legitimately
    differ between the two checked-in copies (e.g. ja adds a comment
    pointing at the projection script). Everything else must match.
    """
    if isinstance(obj, dict):
        return {
            k: _normalise(v) for k, v in obj.items() if not k.startswith("$comment")
        }
    if isinstance(obj, list):
        return [_normalise(x) for x in obj]
    return obj


# Phase 1 PR4: ja-side Layer 2 credential mirror entries that the byte
# check strips from ``worker_roles[*].permissions.deny`` on BOTH sides
# before comparison. Background: the schema's ``$comment_sandbox_anchor``
# documents that ``sandbox.filesystem.denyRead`` entries may carry a
# ``layer2Fallback`` string intended as the Layer-2 ``permissions.deny``
# mirror for when the Layer-3 entry is suppressed on
# WSL / symlink-escape platforms. The pinned runtime versions do NOT
# auto-mirror ``layer2Fallback`` into ``permissions.deny``, so PR4
# (Codex round-1 review) added the home-anchored credential mirrors
# (``Read(~/.aws/*)`` / ``Read(~/.ssh/*)`` and the full credential set
# for doc-audit) to ja's schema. The runtime bundled schema does not
# ship these mirrors — they are ja-side org policy, equivalent to the
# concrete ``sandbox_by_pattern`` bodies stripped above. Listing them
# here lets the byte check stay focused on the schema *surface* contract
# while the semantic fixtures (``schema_source: "shipped"``) keep
# end-to-end coverage of the mirrors. New mirror entries added by future
# ja-only Layer-2 policy work should be added to this set.
_JA_ONLY_LAYER2_CREDENTIAL_DENIES = frozenset(
    {
        "Read(.env)",
        "Read(.env.*)",
        "Read(**/credentials*)",
        "Read(**/*.pem)",
        "Read(~/.config/gh/hosts.yml)",
        "Read(~/.aws/*)",
        "Read(~/.ssh/*)",
    }
)


def _strip_ja_only_sandbox_bodies(schema: object) -> object:
    """Drop ja-only ``sandbox`` / ``sandbox_by_pattern`` bodies and ja-only
    Layer 2 credential mirror entries from each role / worker_role.

    Phase 1 PR3 (Refs claude-org-ja#378 #376) lands concrete
    ``sandbox`` bodies on ``roles.{secretary,dispatcher,curator}`` and
    Phase 1 PR4 lands concrete ``sandbox_by_pattern`` bodies on
    ``worker_roles.{default,claude-org-self-edit,doc-audit}``. Both are
    driven by the Phase 0 contract at
    ``docs/contracts/role-pattern-sandbox-contract.md``. PR4 also adds
    home-anchored credential mirrors (``Read(~/.aws/*)`` / ``Read(~/.ssh/*)``
    on default/self-edit, the full credential set on doc-audit) to
    ``worker_roles[*].permissions.deny`` to compensate for runtime not
    auto-mirroring ``layer2Fallback`` strings (Codex round-1 review).
    The ``claude-org-runtime`` bundled schema only carries the
    *structural* sandbox surface (Phase 1 PR1 + PR4 sandbox_by_pattern
    shape) — concrete bodies AND the credential mirror set are ja-side
    org policy, not runtime template defaults, and intentionally do not
    ship in the runtime package. The byte check would otherwise flag
    this as drift on every PR3+ / PR4+ commit.

    The semantic check (``--semantic``) keeps end-to-end coverage of
    the in-tree concrete bodies via the
    ``tests/fixtures/runtime_schema_drift/sandbox_intent/role_*.json``
    and ``worker_*.json`` fixtures (``schema_source: "shipped"``), so
    stripping here does not lose verification — it just lets the byte
    check focus on the schema *surface* contract, which is where ja
    and runtime must agree.
    """
    if not isinstance(schema, dict):
        return schema
    out: dict[str, Any] = {}
    for top_key, top_val in schema.items():
        if top_key not in ("roles", "worker_roles") or not isinstance(top_val, dict):
            out[top_key] = top_val
            continue
        new_bucket: dict[str, Any] = {}
        for role_name, role_def in top_val.items():
            if not isinstance(role_def, dict):
                new_bucket[role_name] = role_def
                continue
            cleaned = {
                k: v
                for k, v in role_def.items()
                if k not in ("sandbox", "sandbox_by_pattern")
            }
            # Strip ja-only Layer 2 credential mirrors from
            # ``permissions.deny`` (worker_roles only — org roles
            # carry their credential denies in ``required_deny`` /
            # ``required_allow`` which the byte check already handles
            # symmetrically because PR3 secretary/curator deliberately
            # match runtime baseline modulo the stripped sandbox body).
            if top_key == "worker_roles":
                permissions = cleaned.get("permissions")
                if isinstance(permissions, dict) and isinstance(
                    permissions.get("deny"), list
                ):
                    cleaned = {
                        **cleaned,
                        "permissions": {
                            **permissions,
                            "deny": [
                                entry
                                for entry in permissions["deny"]
                                if entry
                                not in _JA_ONLY_LAYER2_CREDENTIAL_DENIES
                            ],
                        },
                    }
            new_bucket[role_name] = cleaned
        out[top_key] = new_bucket
    return out


def _build_realpath_fn(
    rules: list[dict[str, str]],
) -> Callable[[str], str]:
    """Build a deterministic ``realpath`` shim from fixture rules.

    Each rule is a ``{"prefix", "replacement"}`` pair. For an input path
    ``p`` the first matching rule wins: a rule matches when ``p`` equals
    ``prefix`` or starts with ``prefix + "/"``; the matched prefix is
    swapped for ``replacement``. Paths with no matching rule pass
    through unchanged. The behaviour mirrors the fake-realpath stubs
    used by the runtime's own settings-generator unit tests.
    """
    compiled = list(rules or [])

    def _realpath(p: str) -> str:
        for rule in compiled:
            prefix = rule["prefix"]
            replacement = rule["replacement"]
            if p == prefix:
                return replacement
            if p.startswith(prefix + "/"):
                return replacement + p[len(prefix):]
        return p

    return _realpath


def _filesystem_uses_home_anchor(filesystem: Any) -> bool:
    """Return True iff a filesystem block has any anchor='home' deny entry.

    Helper for :func:`_schema_role_uses_home_anchor` so the home-anchor
    scan can be applied to either the legacy single ``sandbox.filesystem``
    block or each ``sandbox_by_pattern.{A,B,C}.filesystem`` block.
    """
    if not isinstance(filesystem, dict):
        return False
    for layer_key in ("denyRead", "denyWrite"):
        for entry in filesystem.get(layer_key) or []:
            if isinstance(entry, dict) and entry.get("anchor") == "home":
                return True
    return False


def _schema_role_uses_home_anchor(
    schema: dict[str, Any], role_kind: str, role: str
) -> bool:
    """Return True iff the resolved role declares any deny entry with anchor=home.

    Used by the fixture loader to enforce that any fixture rendering a
    role whose sandbox body uses ``anchor: "home"`` must set
    ``inputs.home_dir`` explicitly. Without that the host's real
    ``$HOME`` leaks into the suppression record's ``realpath`` and the
    fixture's ``expected_explain`` becomes host-dependent — exactly the
    portability gap the README warns about.

    Phase 1 PR4: scans both the legacy single ``sandbox.filesystem``
    block and every ``sandbox_by_pattern.{A,B,C}.filesystem`` block on
    worker roles. Without the per-pattern recursion, a fixture that
    omits ``home_dir`` while rendering a role whose Pattern A/B/C body
    declares anchor='home' would silently render with host ``$HOME``
    and the ``expected_explain`` would not be portable across
    machines (the original Codex Major 1 finding).
    """
    bucket_key = "roles" if role_kind == "org" else "worker_roles"
    role_def = (schema.get(bucket_key) or {}).get(role)
    if not isinstance(role_def, dict):
        return False
    sandbox = role_def.get("sandbox")
    if isinstance(sandbox, dict) and _filesystem_uses_home_anchor(
        sandbox.get("filesystem")
    ):
        return True
    sandbox_by_pattern = role_def.get("sandbox_by_pattern")
    if isinstance(sandbox_by_pattern, dict):
        for pattern_body in sandbox_by_pattern.values():
            if isinstance(pattern_body, dict) and _filesystem_uses_home_anchor(
                pattern_body.get("filesystem")
            ):
                return True
    return False


def _resolve_fixture_schema(inputs: dict[str, Any]) -> dict[str, Any]:
    """Resolve which schema dict to feed the renderer for this fixture.

    Two forms are supported:

    - ``inputs.schema_fragment``: an inline mini-schema (the legacy form,
      useful for tightly-scoped evaluator coverage that is independent of
      the current shipped contents of ``tools/org_extension_schema.json``).
    - ``inputs.schema_source = "shipped"``: load the in-tree
      ``tools/org_extension_schema.json`` and feed it to the renderer.
      This is what the Phase 1 PR3 ``role_secretary`` / ``role_dispatcher``
      / ``role_curator`` fixtures use to verify the *actual* concrete
      sandbox bodies as shipped — without it the fixtures would only
      exercise hand-rolled mini-schemas and could silently drift from the
      body operators see.

    Exactly one of the two must be set; passing both is rejected to keep
    the fixture's intent unambiguous.
    """
    has_fragment = "schema_fragment" in inputs
    source = inputs.get("schema_source")
    if has_fragment and source is not None:
        raise ValueError(
            "fixture must set exactly one of inputs.schema_fragment or "
            "inputs.schema_source, not both"
        )
    if source is not None:
        if source != "shipped":
            raise ValueError(
                f"unknown schema_source: {source!r}; "
                "only 'shipped' is currently supported"
            )
        return json.loads(JA_SCHEMA.read_text(encoding="utf-8"))
    if not has_fragment:
        raise ValueError(
            "fixture must set inputs.schema_fragment (inline) "
            "or inputs.schema_source = 'shipped' (read "
            "tools/org_extension_schema.json)"
        )
    return inputs["schema_fragment"]


def _render_fixture_result(fixture: dict[str, Any]) -> Any:
    """Render one fixture and return the full ``RenderResult``.

    Imports the runtime lazily so a missing install surfaces the same
    way the byte check does.

    Fixtures that exercise ``anchor: home`` entries set ``inputs.home_dir``
    to a stable path. The runtime's ``_anchor_base_path`` resolves the
    home anchor via ``os.path.expanduser('~')`` which reads ``$HOME`` on
    POSIX (and ``$USERPROFILE`` on Windows), so we temporarily swap those
    env vars for the duration of the render and restore them afterwards.
    Without this hook the home-anchored realpath would be host-dependent
    and ``expected_explain`` could not be byte-compared across machines —
    the original fixture set sidestepped this by avoiding ``anchor: home``
    altogether (see fixture-dir README), but the Phase 1 PR3 ``role_*``
    fixtures must verify the shipped credential entries which use
    ``anchor: home``.
    """
    from claude_org_runtime.settings.generator import (  # noqa: PLC0415
        render_role_with_metadata,
    )

    inputs = fixture["inputs"]
    realpath_fn = _build_realpath_fn(inputs.get("realpath_map", []))
    wsl_detected = bool(inputs.get("wsl_detected", False))
    schema = _resolve_fixture_schema(inputs)
    home_dir = inputs.get("home_dir")
    if home_dir is None and _schema_role_uses_home_anchor(
        schema, inputs.get("role_kind", "worker"), inputs["role"]
    ):
        raise ValueError(
            f"fixture for role {inputs['role']!r} (role_kind="
            f"{inputs.get('role_kind', 'worker')!r}) must set "
            "inputs.home_dir because the role's sandbox body declares "
            "at least one deny entry with anchor='home' — without the "
            "swap the host's $HOME would leak into the suppression "
            "record realpath and expected_explain would not be portable."
        )

    def _do_render() -> Any:
        return render_role_with_metadata(
            schema,
            role=inputs["role"],
            worker_dir=inputs["worker_dir"],
            claude_org_path=inputs["claude_org_path"],
            role_kind=inputs.get("role_kind", "worker"),
            base_clone=inputs.get("base_clone"),
            task_id=inputs.get("task_id"),
            branch_ref=inputs.get("branch_ref"),
            pattern=inputs.get("pattern"),
            realpath_fn=realpath_fn,
            wsl_detector=lambda: wsl_detected,
        )

    if home_dir is None:
        return _do_render()
    if not isinstance(home_dir, str):
        raise ValueError(
            f"inputs.home_dir must be a string, got {type(home_dir).__name__}"
        )
    # Swap HOME and USERPROFILE so os.path.expanduser('~') is
    # deterministic. Restore-on-finally is mandatory: a stray
    # exception leaving HOME pointed at the fixture's fake path
    # would make every subsequent test render diverge.
    _swap_keys = ("HOME", "USERPROFILE")
    original = {k: os.environ.get(k) for k in _swap_keys}
    for k in _swap_keys:
        os.environ[k] = home_dir
    try:
        return _do_render()
    finally:
        for k, v in original.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _render_fixture_explain(fixture: dict[str, Any]) -> dict[str, Any]:
    """Render one fixture and return the canonical explain JSON."""
    return _render_fixture_result(fixture).sandbox.to_jsonable()


def _render_fixture_rendered_sandbox(fixture: dict[str, Any]) -> Any:
    """Render one fixture and return the rendered ``sandbox`` dict.

    The explain JSON only describes *suppressed* deny entries; the
    *kept* deny entries (and the rest of the sandbox body) live on
    ``result.settings.sandbox``. Without comparing the rendered body
    a regression that drops e.g. ``denyWrite: tools`` from the
    dispatcher would not be detected by ``expected_explain`` alone.
    Fixtures that want kept-entry coverage set ``expected_rendered_sandbox``.
    """
    return _render_fixture_result(fixture).settings.get("sandbox")


def _format_explain_diff(
    fixture_path: Path,
    expected: dict[str, Any],
    actual: dict[str, Any],
) -> str:
    expected_text = json.dumps(expected, indent=2, sort_keys=True).splitlines(
        keepends=True
    )
    actual_text = json.dumps(actual, indent=2, sort_keys=True).splitlines(
        keepends=True
    )
    diff = difflib.unified_diff(
        expected_text,
        actual_text,
        fromfile=f"expected ({fixture_path.name})",
        tofile=f"actual ({fixture_path.name})",
    )
    return "".join(diff)


def _check_byte_drift(installed_str: str) -> int:
    bundled_path = _bundled_schema_path()
    if not bundled_path.is_file():
        print(
            f"check_runtime_schema_drift: bundled schema not found at "
            f"{bundled_path}",
            file=sys.stderr,
        )
        return 1
    if not JA_SCHEMA.is_file():
        print(
            f"check_runtime_schema_drift: ja schema not found at {JA_SCHEMA}",
            file=sys.stderr,
        )
        return 1

    bundled = json.loads(bundled_path.read_text(encoding="utf-8"))
    ja = json.loads(JA_SCHEMA.read_text(encoding="utf-8"))

    bundled_norm = _normalise(_strip_ja_only_sandbox_bodies(bundled))
    ja_norm = _normalise(_strip_ja_only_sandbox_bodies(ja))
    if bundled_norm == ja_norm:
        print(
            f"check_runtime_schema_drift: OK (claude-org-runtime "
            f"{installed_str} bundled schema matches {JA_SCHEMA.name})"
        )
        return 0

    print(
        "check_runtime_schema_drift: DRIFT — ja's "
        f"{JA_SCHEMA.name} differs from the schema bundled with "
        f"claude-org-runtime {installed_str} ({bundled_path}).",
        file=sys.stderr,
    )
    print(
        "  Either update tools/org_extension_schema.json to match the "
        "runtime, or release a new runtime that matches ja's schema, "
        "then re-pin in pyproject.toml + requirements.txt.",
        file=sys.stderr,
    )
    return 1


_FIXTURE_OUT_OF_SCOPE_FIELDS = ("verification_depth",)


def _find_forbidden_keys(
    obj: Any, forbidden: tuple[str, ...]
) -> list[tuple[str, str]]:
    """Walk ``obj`` and return ``(json_pointer, key)`` for every
    forbidden key found at any depth.

    Recurses through dicts and lists; ignores everything else. The
    ``json_pointer`` is a slash-joined path so violations point at
    the offending location even when the forbidden key is nested
    inside ``inputs.schema_fragment.worker_roles.<role>.…``.
    """
    hits: list[tuple[str, str]] = []

    def _walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                child_path = f"{path}/{key}" if path else key
                if key in forbidden:
                    hits.append((child_path, key))
                _walk(value, child_path)
        elif isinstance(node, list):
            for idx, value in enumerate(node):
                _walk(value, f"{path}/{idx}")

    _walk(obj, "")
    return hits


def _validate_fixture_policy(
    fixture_path: Path, fixture: dict[str, Any]
) -> list[str]:
    """Return a list of policy violations for one fixture.

    Currently enforces only that the out-of-scope fields listed in
    ``_FIXTURE_OUT_OF_SCOPE_FIELDS`` (notably ``verification_depth``,
    which is a delegate-payload convention rather than a sandbox
    enforcement dimension) do not appear *anywhere* in the fixture —
    not just at the top level of ``inputs`` / ``expected_explain``,
    but also nested inside ``schema_fragment``. A shallow check would
    let a fixture sneak ``verification_depth`` into the schema body
    and silently establish a precedent the rest of the codebase has
    to maintain. Mirrors the policy check in
    ``tests/test_runtime_schema_drift_semantic.py`` so the manual CLI
    run gives the same answer as the unittest suite.
    """
    violations: list[str] = []
    hits = _find_forbidden_keys(fixture, _FIXTURE_OUT_OF_SCOPE_FIELDS)
    for path, key in hits:
        violations.append(
            f"{fixture_path.name}: {key!r} must not appear in fixture "
            f"(found at {path!r}; out-of-scope for sandbox semantic "
            "contract; see fixture-dir README)."
        )
    return violations


def _check_semantic_drift(installed_str: str) -> int:
    if not SEMANTIC_FIXTURE_DIR.is_dir():
        print(
            f"check_runtime_schema_drift: semantic fixture dir not found at "
            f"{SEMANTIC_FIXTURE_DIR}",
            file=sys.stderr,
        )
        return 1
    fixture_paths = sorted(SEMANTIC_FIXTURE_DIR.glob("*.json"))
    if not fixture_paths:
        print(
            f"check_runtime_schema_drift: no semantic fixtures found in "
            f"{SEMANTIC_FIXTURE_DIR}",
            file=sys.stderr,
        )
        return 1
    drift_seen = False
    policy_violations: list[str] = []
    for fixture_path in fixture_paths:
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        policy_violations.extend(_validate_fixture_policy(fixture_path, fixture))
        # Render once; both checks read from the same RenderResult so a
        # transient env-var swap (home_dir) doesn't have to happen twice.
        result = _render_fixture_result(fixture)
        expected = fixture["expected_explain"]
        actual = result.sandbox.to_jsonable()
        if expected != actual:
            drift_seen = True
            print(
                "check_runtime_schema_drift: SEMANTIC DRIFT — "
                f"{fixture_path.relative_to(REPO_ROOT)} explain JSON differs "
                f"from claude-org-runtime {installed_str} render output.",
                file=sys.stderr,
            )
            diff = _format_explain_diff(fixture_path, expected, actual)
            if diff:
                print(diff, file=sys.stderr)
        if "expected_rendered_sandbox" in fixture:
            expected_sandbox = fixture["expected_rendered_sandbox"]
            actual_sandbox = result.settings.get("sandbox")
            if expected_sandbox != actual_sandbox:
                drift_seen = True
                print(
                    "check_runtime_schema_drift: SEMANTIC DRIFT — "
                    f"{fixture_path.relative_to(REPO_ROOT)} rendered sandbox "
                    f"body differs from claude-org-runtime {installed_str} "
                    "output (kept deny entries / additionalDirectories).",
                    file=sys.stderr,
                )
                diff = _format_explain_diff(
                    fixture_path, expected_sandbox, actual_sandbox
                )
                if diff:
                    print(diff, file=sys.stderr)
    if policy_violations:
        for v in policy_violations:
            print(
                f"check_runtime_schema_drift: FIXTURE POLICY — {v}",
                file=sys.stderr,
            )
    if drift_seen:
        print(
            "  Update the affected fixture(s) under "
            f"{SEMANTIC_FIXTURE_DIR.relative_to(REPO_ROOT)}/ if the runtime "
            "behaviour change is intended, or fix the runtime if it is not.",
            file=sys.stderr,
        )
        return 1
    if policy_violations:
        return 1
    print(
        f"check_runtime_schema_drift: semantic OK (claude-org-runtime "
        f"{installed_str}, {len(fixture_paths)} fixture(s))"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Drift check between ja's tools/org_extension_schema.json and "
            "the bundled claude-org-runtime schema. With no flags runs the "
            "byte-identical check; --semantic switches to the render-output "
            "golden diff against fixture explain JSON; pass --byte "
            "--semantic together to run both."
        ),
    )
    parser.add_argument(
        "--semantic",
        action="store_true",
        help=(
            "Run the semantic golden drift check against fixtures under "
            "tests/fixtures/runtime_schema_drift/sandbox_intent/. "
            "Without --byte this replaces the byte check; combine with "
            "--byte to run both."
        ),
    )
    parser.add_argument(
        "--byte",
        action="store_true",
        help=(
            "Run the byte-identical schema check (the default when no "
            "flags are passed). Combine with --semantic to run both."
        ),
    )
    args = parser.parse_args(argv)

    run_byte = args.byte or not args.semantic
    run_semantic = args.semantic

    try:
        installed_str = _pkg_version("claude-org-runtime")
    except PackageNotFoundError:
        print(
            "check_runtime_schema_drift: claude-org-runtime is not installed; "
            "run `pip install -e .` first.",
            file=sys.stderr,
        )
        return 1
    installed = _parse_version(installed_str)
    in_window = _runtime_in_pin_window(installed)

    rc = 0
    if run_byte:
        # Byte check honours the pin window: a runtime preview release
        # is allowed to ship a schema ahead of ja's pin, so a hard
        # failure here would just block unrelated PRs. Skip with a
        # warning when out-of-window.
        if not in_window:
            print(
                f"check_runtime_schema_drift: WARN — installed "
                f"claude-org-runtime {installed_str} is outside ja's pin "
                f"window "
                f">={'.'.join(map(str, RUNTIME_PIN_LOWER_INCLUSIVE))},"
                f"<{'.'.join(map(str, RUNTIME_PIN_UPPER_EXCLUSIVE[:2]))}; "
                "skipping byte drift check (runtime is allowed to ship a "
                "new minor before ja widens the pin)."
            )
        else:
            rc = _check_byte_drift(installed_str) or rc
    if run_semantic:
        # Semantic check runs unconditionally when explicitly
        # requested. Operators invoking `--semantic` against a
        # 0.2-preview runtime want exactly this answer ("does the
        # explain JSON still match my goldens against the new
        # evaluator?"), and silently skipping would defeat the point
        # of the flag. The matching pytest test is the same shape:
        # it always runs against whatever runtime is importable.
        rc = _check_semantic_drift(installed_str) or rc
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
