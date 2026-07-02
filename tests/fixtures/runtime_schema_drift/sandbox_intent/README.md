# Sandbox-intent semantic drift fixtures

Inputs for the semantic golden drift check in
[`tools/check_runtime_schema_drift.py`](../../../../tools/check_runtime_schema_drift.py).
Each fixture pairs a small input schema fragment with the expected
`SandboxMetadata.to_jsonable()` (the "explain JSON") output of
`claude_org_runtime.settings.generator.render_role_with_metadata()`.
The byte-identical schema check (existing path) catches structural
divergence; this semantic check catches behavioural divergence in the
suppression evaluator on top of identical schema bytes.

## Fixture format

```jsonc
{
  "description": "what this fixture exercises",
  "inputs": {
    "role": "...",                // required, key inside schema's worker_roles or roles
    "role_kind": "worker"|"org",  // optional, defaults to "worker"
    "worker_dir": "...",          // required absolute path
    "claude_org_path": "...",     // required absolute path
    "base_clone": "...",          // optional, Pattern B placeholder
    "task_id": "...",             // optional, Pattern B placeholder
    "branch_ref": "...",          // optional, Pattern B placeholder
    "pattern": "A"|"B"|"C"|null,  // optional, informational only
    "wsl_detected": true|false,   // stub for runtime's wsl_detector
    "realpath_map": [             // fake realpath rules; first match wins
      {"prefix": "/some/path", "replacement": "/another/path"}
    ],

    // Choose exactly one of the next two — the loader rejects setting
    // both, and rejects setting neither.
    "schema_fragment": { ... },   // (a) inline mini-schema dict
    "schema_source": "shipped",   // (b) load tools/org_extension_schema.json

    "home_dir": "/H"              // optional; required if any deny entry uses
                                  //   anchor:"home". Swaps $HOME/$USERPROFILE
                                  //   for the duration of the render so
                                  //   os.path.expanduser('~') is deterministic.
  },
  "expected_explain": { ... },    // SandboxMetadata.to_jsonable() output

  "expected_rendered_sandbox": {  // optional; rendered RenderResult.settings.sandbox
    "enabled": true, ...          // (kept deny entries + additionalDirectories,
  }                               // post-substitution, suppressed entries dropped).
                                  // Set when the fixture wants to detect a
                                  // regression that drops a kept entry — the
                                  // explain JSON only describes *suppressions*,
                                  // so without this a removed `denyWrite` would
                                  // not be detected if its suppression state
                                  // didn't change.
}
```

As of `claude-org-runtime` 0.1.30 the rendered `filesystem.denyRead` /
`denyWrite` arrays are **resolved absolute-path strings** (anchor +
realpath applied), not the structured
`{anchor, path, suppressOnSymlinkEscape, layer2Fallback}` dicts that the
schema *input* carries. The structured metadata (including
`layer2Fallback`) is retained only on the explain JSON's suppression
records — see the `layer2Fallback` note below. Fixtures written for
runtimes before 0.1.30 carried the dict shape in
`expected_rendered_sandbox` and must be regenerated for 0.1.30+ (see
"Bulk-updating fixtures for a new runtime version").

`realpath_map` rules apply to a path `p` when `p == prefix` or
`p.startswith(prefix + "/")`. The matched prefix is replaced and the
result is returned. Paths that match no rule pass through unchanged.

### `schema_source: "shipped"` vs inline `schema_fragment`

- Use `schema_fragment` when the fixture's purpose is to exercise a
  specific evaluator path with a minimal, hand-rolled mini-schema —
  the original sandbox_default / sandbox_doc_audit /
  sandbox_pattern_b_self_edit fixtures use this form.
- Use `schema_source: "shipped"` when the fixture's purpose is to
  pin behaviour against the *actual concrete sandbox body* that ships
  in [`tools/org_extension_schema.json`](../../../../tools/org_extension_schema.json).
  The Phase 1 PR3 `role_secretary.json` / `role_dispatcher.json` /
  `role_curator.json` fixtures use this form so the concrete bodies
  for the three org roles are verified, not just the evaluator. A
  hand-rolled mini-schema cannot detect a regression in the shipped
  body; the shipped form can.

### `layer2Fallback` is forward-looking, not auto-mirrored today

A structured deny entry may carry a `layer2Fallback` string per
`worker_roles.$comment_sandbox_anchor` in the schema. The intent is
that when the Layer-3 entry is suppressed (e.g. on WSL the home-
anchored `~/.aws/**` deny escapes sandbox read roots), the runtime
mirrors the fallback string into `permissions.deny`. The version of
`claude-org-runtime` pinned by this repo does NOT yet implement that
mirror — the `layer2Fallback` field is preserved on the suppression
record's `entry` but not emitted into `permissions.deny`. Fixtures
therefore only compare suppressions and the rendered sandbox dict;
they do not assert anything about `permissions.deny`. Effective
Layer 2 deny for credentials still has to be declared by hand via
`roles.<role>.required_deny`.

### `home_dir` and `anchor: "home"`

The runtime resolves `anchor: "home"` via `os.path.expanduser("~")`,
which on POSIX reads `$HOME` (Windows: `$USERPROFILE`) — host-dependent
by default. Setting `inputs.home_dir = "/H"` (or any stable path) makes
the loader temporarily swap those env vars for the duration of the
render so home-anchored entries produce a byte-portable
`expected_explain`. Restore-on-finally is unconditional: a stray
exception cannot leak the fake `$HOME` into other tests.

Fixtures that do not use `anchor: "home"` should omit `home_dir`.

## Out-of-scope intentionally

- **`verification_depth`**: this is a delegate-payload / brief
  convention surfaced via `tools/gen_delegate_payload.py`, not a
  sandbox enforcement dimension. The renderer's
  `render_role_with_metadata()` does not branch on it and the explain
  JSON does not include it. Fixtures here MUST NOT add a
  `verification_depth` field to either `inputs` or
  `expected_explain` — depth is enforcement-out-of-scope by design.
- ~~**Concrete sandbox bodies for `worker_roles.*`**: not landed in the
  Phase 1 PR3 changeset~~ — landed in Phase 1 PR4 via the
  `worker_<role>_<pattern>.json` fixtures (per the Codex pre-design
  review's per-pattern file convention: 1 fixture = 1 pattern render,
  no nested-fixture-object form). See "Per-pattern fixtures" below.

## Per-pattern fixtures (Phase 1 PR4)

`worker_roles.<role>.sandbox_by_pattern.{A,B,C}` is a per-pattern
sandbox surface — the runtime selects the body keyed by `--pattern` at
render time (per [`role_configs_schema.json`](https://pypi.org/project/claude-org-runtime/)
`$comment_sandbox_by_pattern`). To pin behaviour without losing per-
pattern coverage, each shipped worker role × pattern combination has a
dedicated fixture file:

- `worker_default_A.json` / `worker_default_B.json` / `worker_default_C.json`
  — `worker_roles.default`, all three patterns.
- `worker_self_edit_B.json` / `worker_self_edit_C.json` — Pattern A is
  intentionally omitted because the resolver
  ([`tools/resolve_worker_layout.py:643`](../../../../tools/resolve_worker_layout.py))
  refuses `pattern=A` for `claude-org-self-edit` (would break the
  live-repo single-`.git` invariant from Issue #289).
- `worker_doc_audit_A.json` / `worker_doc_audit_B.json` / `worker_doc_audit_C.json`
  — doc-audit is pattern-orthogonal in WRITE intent (the read-only
  audit constraint dominates regardless of pattern) but pattern-
  dependent in READ surface per Phase 0 §4.6.1 ('Identical to the
  underlying pattern'). Pattern B's body therefore additionally mounts
  the base_clone Git metadata carve-outs from §4.2.1 (worktrees /
  objects / refs/heads/<branch_ref> / packed-refs) so doc-audit's
  allowed `git status` / `diff` / `log` Bash commands can resolve
  `<worker_dir>/.git`'s gitdir pointer; explicit denyWrite entries
  keep those mounts read-only on top of the broader
  `denyWrite[{worker_dir}/**]`. Pattern A and C bodies are simpler
  (worker_dir auto-mount covers their .git location).

Each fixture sets `inputs.schema_source = "shipped"` so it pins the
*actual concrete body* in
[`tools/org_extension_schema.json`](../../../../tools/org_extension_schema.json),
not a hand-rolled mini-schema. `inputs.pattern` selects the
`sandbox_by_pattern[pattern]` body. Pattern B fixtures additionally
set `base_clone` / `task_id` / `branch_ref` so `{base_clone}` /
`{task_id}` / `{branch_ref}` placeholders resolve deterministically.

The single-pattern-per-file convention is intentional: a nested
fixture object form (e.g. `expected_explain_by_pattern: {A, B, C}`)
would have required widening the loader, the validation, the diff
formatter, and the pytest companion all in one go (Codex Major 3 —
the per-file form is preferred over a nested-object form). Adding a
new pattern coverage row is just a matter of dropping a new fixture
file.

## Adding a new fixture

1. Drop the JSON file into this directory.
2. Run `python tools/check_runtime_schema_drift.py --semantic` and
   confirm it prints OK. Mismatches print a unified diff between the
   committed `expected_explain` and the runtime's actual output.
3. The pytest companion at
   [`tests/test_runtime_schema_drift_semantic.py`](../../../test_runtime_schema_drift_semantic.py)
   discovers fixtures by glob, so no test wiring is needed.

## Bulk-updating fixtures for a new runtime version

When `claude-org-runtime` ships a new release whose
`render_role_with_metadata()` changes the *shape* of the rendered
explain / sandbox JSON (e.g. 0.1.30's denyRead/denyWrite dict -> resolved
string normalization), every `expected_explain` /
`expected_rendered_sandbox` golden here goes red at once. Do **not**
blindly overwrite the goldens with the new runtime's raw output — a
schema regression would be laundered into the fixtures silently. Follow
this procedure:

1. **Confirm the installed runtime version** you are adopting:
   `python -c "from importlib.metadata import version; print(version('claude-org-runtime'))"`.
   This is the version the goldens will be pinned to.
2. **Semantic-diff review first.** Run
   `python -m pytest tests/test_runtime_schema_drift_semantic.py -q` (or
   `python tools/check_runtime_schema_drift.py --semantic`) and read the
   printed `expected` vs `actual` diff for each fixture. Classify every
   change as an *intended* schema/evaluator change (documented in the
   runtime release notes / changelog) or an *unexpected* regression.
   Only proceed if all diffs are intended. Record the vocabulary of the
   change (which keys moved / restructured) for the PR description.
3. **Regenerate or incorporate.** This repo is an experimental fork of
   `claude-org-ja`; if ja already carries the same fixtures updated for
   the target runtime, prefer a selective incorporation of ja's
   fixture-regen commit (verify each local fixture is byte-identical to
   ja's pre-regen version first, so no fork-local change is machine-
   overwritten) over a from-scratch regeneration. Regenerate from the
   live runtime only when incorporation does not apply cleanly.
4. **Bump the pin floor to match.** The regenerated goldens are only
   valid at `>= <target version>`; bump the floor in `pyproject.toml`,
   `requirements.txt`, and `RUNTIME_PIN_LOWER_INCLUSIVE` in
   [`tools/check_runtime_schema_drift.py`](../../../../tools/check_runtime_schema_drift.py)
   (and any smoke-test floor that mirrors the pin) so a below-floor
   install can't render the old shape and fail these tests. Leave the
   upper bound (`<0.2`) alone unless a separate decision widens it.
5. **Byte + semantic + test.** Re-run
   `python tools/check_runtime_schema_drift.py --byte --semantic` and
   `python -m pytest tests/test_runtime_schema_drift_semantic.py` and
   confirm both are green before committing.
