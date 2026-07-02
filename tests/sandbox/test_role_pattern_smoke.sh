#!/usr/bin/env bash
# Smoke tests for Linux/WSL2 role × pattern sandbox surface.
#
# Verifies the *scriptable* portions of the acceptance criteria in
# docs/runbooks/linux-sandbox-verification.md (claude-org-ja#380):
#   - §1.1 prerequisite tooling (bwrap / socat / jq) — host capability.
#   - §3 / §6 schema integrity — worker_roles.default declares
#     sandbox_by_pattern A/B/C with credential denyRead, the Layer 2
#     mirror lives in worker_roles.default.permissions.deny, and the
#     Phase 2 hook attach (PR #420) wired block-no-verify /
#     block-dangerous-git into worker_roles.default.hooks.PreToolUse[Bash].
#   - §4 / §6.1 / §6.2 hook behavior — direct PreToolUse invocation of
#     check-worker-boundary.sh / block-org-structure.sh /
#     block-dispatcher-out-of-scope.sh / block-no-verify.sh /
#     block-dangerous-git.sh against the JSON shapes Claude Code core
#     would deliver.
#
# Manual / out-of-scope rows (require live Claude Code spawn or bwrap-
# launched subprocess) stay in the runbook: /sandbox status display,
# Pattern B commit smoke against a real worktree, syscall-level denyRead
# enforcement, dispatcher pane bypassPermissions × Layer 4 isolation.
#
# Output format: TAP-ish lines + a "# N passed, M failed" summary line so
# tests/run-all.sh can aggregate.
#
# Reuses the env/exit/IO patterns from
# tests/test-check-worker-boundary.sh and
# tests/test-block-pretooluse-hooks.sh.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# This script lives in tests/sandbox/, so REPO_ROOT is two levels up.
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
HOOK_DIR="$REPO_ROOT/.hooks"
SCHEMA="$REPO_ROOT/tools/org_extension_schema.json"

PASS=0; FAIL=0; TEST_NUM=0
TMPFILES=()
cleanup() { rm -f "${TMPFILES[@]}"; }
trap cleanup EXIT

ok()       { ((TEST_NUM++)); echo "ok $TEST_NUM - $1"; ((PASS++)); }
not_ok()   { ((TEST_NUM++)); echo "not ok $TEST_NUM - $1"; ((FAIL++)); }
# Soft-skip: counts as pass so CI without sandbox prereqs (bwrap/socat) or
# without the Python venv (claude-org-runtime) still reports success on
# the schema / hook assertions, which are the meaningful regression
# surface of this smoke. The runbook (§1) covers the manual prereq
# check; this script's role is to catch *schema* drift and *hook* drift,
# not to gate the harness on a fully-provisioned sandbox host.
skip_msg() { ((TEST_NUM++)); echo "ok $TEST_NUM - # SKIP $1"; ((PASS++)); }

assert_exit() {
  local expected="$1" actual="$2" desc="$3"
  if [[ "$actual" -eq "$expected" ]]; then
    ok "$desc"
  else
    not_ok "$desc (expected exit $expected, got $actual)"
  fi
}

assert_jq_true() {
  local desc="$1"; shift
  local expr="$1"; shift
  local result
  if ! result=$(jq -e "$expr" "$SCHEMA" 2>/dev/null); then
    not_ok "$desc (jq expression failed: $expr)"
    return
  fi
  if [[ "$result" == "true" ]]; then
    ok "$desc"
  else
    not_ok "$desc (jq returned: $result)"
  fi
}

# Portable realpath (matches the helper used by the hooks themselves).
portable_realpath() {
  local target="$1"
  if result=$(command realpath -m "$target" 2>/dev/null); then
    echo "$result"
  elif result=$(python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" "$target" 2>/dev/null); then
    echo "$result"
  elif result=$(python -c "import os,sys; print(os.path.realpath(sys.argv[1]))" "$target" 2>/dev/null); then
    echo "$result"
  else
    echo "FATAL: realpath -m / python both unavailable" >&2
    exit 1
  fi
}

WORKER_DIR_FOR_HOOKS="$(portable_realpath "$REPO_ROOT")"
CLAUDE_ORG_PATH_FOR_HOOKS="$(portable_realpath "$REPO_ROOT/../..")"

run_hook_with_env() {
  local hook="$1" json="$2" stderr_file="$3"
  local exit_code=0
  echo "$json" \
    | env WORKER_DIR="$WORKER_DIR_FOR_HOOKS" \
          CLAUDE_ORG_PATH="$CLAUDE_ORG_PATH_FOR_HOOKS" \
      bash "$hook" 2>"$stderr_file" \
    || exit_code=$?
  echo "$exit_code"
}

run_hook_org_only() {
  local hook="$1" json="$2" stderr_file="$3"
  local exit_code=0
  echo "$json" \
    | env CLAUDE_ORG_PATH="$CLAUDE_ORG_PATH_FOR_HOOKS" \
      bash "$hook" 2>"$stderr_file" \
    || exit_code=$?
  echo "$exit_code"
}

mktmp_stderr() {
  local f
  f=$(mktemp)
  TMPFILES+=("$f")
  echo "$f"
}

# -----------------------------------------------------------------------
# §1. Prerequisite tooling
# -----------------------------------------------------------------------
# jq is a HARD requirement: the .hooks/ scripts under §4 themselves exit 2
# if jq is missing, so we cannot meaningfully run the rest of the smoke
# without it.
#
# bwrap / socat / claude / claude-org-runtime are SOFT requirements: they
# are needed for the runbook's full Layer 3 / E2E verification, but the
# schema-drift and hook-behavior assertions in §3 / §4 do not depend on
# them. CI ([.github/workflows/tests.yml] only installs jq) and partially
# provisioned dev hosts skip these rows rather than failing — the runbook
# §1 is the operator's checklist for the full verification.
echo "# §1 prerequisite tooling"

if command -v jq >/dev/null 2>&1; then
  ok "jq is on \$PATH (hard requirement: hooks depend on jq)"
else
  not_ok "jq is on \$PATH (install: sudo apt-get install jq)"
  echo "# 1 passed, 1 failed (jq unavailable, cannot continue)"
  exit 1
fi

if command -v bwrap >/dev/null 2>&1; then
  ok "bwrap is on \$PATH"
else
  skip_msg "bwrap unavailable — Layer 3 sandbox enforcement falls open. Install: sudo apt-get install bubblewrap (per runbook §1.1)"
fi

if command -v socat >/dev/null 2>&1; then
  ok "socat is on \$PATH"
else
  skip_msg "socat unavailable — required by some Claude Code sandbox features. Install: sudo apt-get install socat (per runbook §1.1)"
fi

if command -v claude >/dev/null 2>&1; then
  # `claude --version` is what the runbook §1.2 instructs operators to
  # run. Verifying the binary actually responds (not just that it is on
  # PATH) catches the case where a broken install leaves the entry on
  # PATH but the binary fails to execute — which would in turn break
  # /sandbox status checks downstream.
  if claude_ver=$(claude --version 2>/dev/null) && [[ -n "$claude_ver" ]]; then
    ok "claude --version succeeded ($claude_ver)"
  else
    skip_msg "claude is on \$PATH but '--version' did not return output — likely a broken install (runbook §1.2)"
  fi
else
  skip_msg "claude unavailable — E2E spawn verification (runbook §3) cannot run from this host"
fi

# claude-org-runtime version check. Prefer the CLI (`claude-org-runtime
# --version`) over `pip show` — `pip show` walks the *system* site-
# packages and can return a stale version that has nothing to do with
# the binary an operator actually invokes when running
# `claude-org-runtime settings generate`. The CLI is what the runbook §1.2
# instructs.
#
# We try the project venv first, then PATH. We accept any 0.1.x ≥0.1.30
# (the requirements.txt pin window). Older or out-of-window versions
# skip the assertion rather than fail because the schema / hook tests
# below do not depend on the runtime; the row exists to surface
# "your runtime is older than requirements.txt expects, sandbox_by_pattern
# may not be emitted into worker settings.local.json" at smoke time.
runtime_version=""    # parseable "X.Y.Z" (no suffix)
runtime_version_src=""  # human-readable source label for the OK/SKIP message
for runtime_cmd in "$REPO_ROOT/.venv/bin/claude-org-runtime" "claude-org-runtime"; do
  if command -v "$runtime_cmd" >/dev/null 2>&1 || [[ -x "$runtime_cmd" ]]; then
    # `claude-org-runtime --version` prints "claude-org-runtime X.Y.Z";
    # take the last whitespace-delimited token.
    if v=$("$runtime_cmd" --version 2>/dev/null | awk '{print $NF}'); then
      if [[ -n "$v" ]]; then
        runtime_version="$v"
        runtime_version_src="$runtime_cmd"
        break
      fi
    fi
  fi
done
# pip show is consulted only if the CLI was unreachable, as a last
# resort. It is intentionally lower-priority because of the stale-
# metadata risk above.
if [[ -z "$runtime_version" ]]; then
  for pip_cmd in "$REPO_ROOT/.venv/bin/pip" "pip3" "pip"; do
    if command -v "$pip_cmd" >/dev/null 2>&1 || [[ -x "$pip_cmd" ]]; then
      if v=$("$pip_cmd" show claude-org-runtime 2>/dev/null | awk -F': ' '/^Version:/ {print $2}'); then
        if [[ -n "$v" ]]; then
          runtime_version="$v"
          runtime_version_src="$pip_cmd show (CLI unreachable)"
          break
        fi
      fi
    fi
  done
fi
if [[ -z "$runtime_version" ]]; then
  skip_msg "claude-org-runtime not installed — cannot verify >=0.1.30,<0.2 pin (runbook §1.2)"
else
  # Compare 0.1.X >= 0.1.30 numerically. We only need to detect "below
  # 0.1.30" or "outside 0.1.x" since the requirements.txt pin window is
  # narrow. Anything in 0.2.x is also out-of-window.
  major=$(echo "$runtime_version" | awk -F. '{print $1+0}')
  minor=$(echo "$runtime_version" | awk -F. '{print $2+0}')
  patch=$(echo "$runtime_version" | awk -F. '{print $3+0}')
  if [[ "$major" -eq 0 && "$minor" -eq 1 && "$patch" -ge 30 ]]; then
    ok "claude-org-runtime $runtime_version satisfies >=0.1.30,<0.2 (source: $runtime_version_src)"
  else
    skip_msg "claude-org-runtime $runtime_version is outside the >=0.1.30,<0.2 pin (source: $runtime_version_src) — sandbox_by_pattern may not be emitted into worker settings (runbook §1.2)"
  fi
fi

# -----------------------------------------------------------------------
# §3. Schema integrity (Phase 1 PR4 sandbox_by_pattern + Phase 2 hook
# attach in PR #420 + Layer 2 credential mirror).
#
# These rows assert what `claude-org-runtime settings generate` is
# expected to project into a worker's .claude/settings.local.json. They
# do not run the generator (that requires .venv setup); the schema is
# the SoT consumed by the generator, so asserting against the schema is
# a tighter and more deterministic test.
# -----------------------------------------------------------------------
echo "# §3 schema integrity"

# 3.a worker_roles.default has sandbox_by_pattern A/B/C with enabled=true.
for pattern in A B C; do
  assert_jq_true \
    "worker_roles.default.sandbox_by_pattern.$pattern.enabled == true" \
    ".worker_roles.default.sandbox_by_pattern.${pattern}.enabled == true"
done

# 3.b Each pattern declares the full credential denyRead set from
# role-pattern-sandbox-contract §4.1.1 (.env, .env.*, **/credentials*,
# **/*.pem). The structured-anchor schema uses entries of shape
# {anchor, path, ...}; we assert each {worker_dir, <path>} entry exists
# for each pattern. Asserting the full set (not just two representatives)
# protects against schema regressions that drop a single credential
# pattern; missing .env.* would silently expose .env.local etc.
for pattern in A B C; do
  for path in '.env' '.env.*' '**/credentials*' '**/*.pem'; do
    assert_jq_true \
      "worker_roles.default.sandbox_by_pattern.$pattern.filesystem.denyRead has worker_dir/$path" \
      "any(.worker_roles.default.sandbox_by_pattern.${pattern}.filesystem.denyRead[]; .anchor == \"worker_dir\" and .path == \"$path\")"
  done
done

# 3.c Pattern B union must include the four worktree git-metadata mounts
# (worktrees/{task_id}, objects, refs/heads/{branch_ref}, packed-refs)
# per role-pattern-sandbox-contract §4.2.1. Without these, Pattern B
# workers fail to commit because git can't write to the per-worktree
# metadata or the shared object store.
assert_jq_true \
  "Pattern B additionalDirectories has {base_clone}/.git/worktrees/{task_id}" \
  'any(.worker_roles.default.sandbox_by_pattern.B.filesystem.additionalDirectories[]; . == "{base_clone}/.git/worktrees/{task_id}")'
assert_jq_true \
  "Pattern B additionalDirectories has {base_clone}/.git/objects" \
  'any(.worker_roles.default.sandbox_by_pattern.B.filesystem.additionalDirectories[]; . == "{base_clone}/.git/objects")'
assert_jq_true \
  "Pattern B additionalDirectories has {base_clone}/.git/refs/heads/{branch_ref}" \
  'any(.worker_roles.default.sandbox_by_pattern.B.filesystem.additionalDirectories[]; . == "{base_clone}/.git/refs/heads/{branch_ref}")'
assert_jq_true \
  "Pattern B additionalDirectories has {base_clone}/.git/packed-refs" \
  'any(.worker_roles.default.sandbox_by_pattern.B.filesystem.additionalDirectories[]; . == "{base_clone}/.git/packed-refs")'

# 3.d Layer 2 credential mirror in worker_roles.default.permissions.deny.
# Even when Layer 3 is suppressed (case E on WSL) or fall-open (no bwrap),
# Read-tool credential access stays blocked at Layer 2. role-pattern-
# sandbox-contract §4.1.2 / §1.3.
for entry in 'Read(.env)' 'Read(.env.*)' 'Read(**/credentials*)' 'Read(**/*.pem)' 'Read(~/.config/gh/hosts.yml)' 'Read(~/.aws/*)' 'Read(~/.ssh/*)'; do
  assert_jq_true \
    "worker_roles.default.permissions.deny includes $entry" \
    "any(.worker_roles.default.permissions.deny[]; . == \"$entry\")"
done

# 3.e Phase 2 hook attach (PR #420): worker_roles.default.hooks.PreToolUse
# Bash matcher must include block-no-verify.sh and block-dangerous-git.sh.
# Before PR #420, default workers running outside the claude-org repo did
# not inherit these hooks (cwd-tree settings non-inheritance — see
# role-pattern-sandbox-contract §4.1.2 «Gap → Phase 1»).
assert_jq_true \
  "worker_roles.default Bash hooks include block-no-verify.sh (Phase 2 attach)" \
  '[.worker_roles.default.hooks.PreToolUse[] | select(.matcher == "Bash") | .hooks[].command] | any(. | test("block-no-verify\\.sh"))'
assert_jq_true \
  "worker_roles.default Bash hooks include block-dangerous-git.sh (Phase 2 attach)" \
  '[.worker_roles.default.hooks.PreToolUse[] | select(.matcher == "Bash") | .hooks[].command] | any(. | test("block-dangerous-git\\.sh"))'

# 3.f repo_shared still requires block-no-verify.sh and block-dangerous-git.sh
# (defense-in-depth for when secretary / dispatcher commit inside the
# claude-org repo).
assert_jq_true \
  "repo_shared.required_hooks includes block-no-verify.sh" \
  'any(.roles.repo_shared.required_hooks[]; .command_contains == "block-no-verify.sh")'
assert_jq_true \
  "repo_shared.required_hooks includes block-dangerous-git.sh" \
  'any(.roles.repo_shared.required_hooks[]; .command_contains == "block-dangerous-git.sh")'

# 3.g Repo-shared .claude/settings.json declares a sandbox.filesystem.denyRead
# block (Phase 1 PR3 surface). This is the secretary's effective Layer 3
# when running with cwd = claude_org_path.
SHARED_SETTINGS="$REPO_ROOT/.claude/settings.json"
if [[ -f "$SHARED_SETTINGS" ]]; then
  if jq -e '.sandbox.filesystem.denyRead | length > 0' "$SHARED_SETTINGS" >/dev/null 2>&1; then
    ok "repo-shared .claude/settings.json declares sandbox.filesystem.denyRead"
  else
    not_ok "repo-shared .claude/settings.json declares sandbox.filesystem.denyRead"
  fi
else
  not_ok "repo-shared .claude/settings.json exists at $SHARED_SETTINGS"
fi

# -----------------------------------------------------------------------
# §4. Hook behavior smoke (PreToolUse direct invocation).
#
# These rows feed the hooks the same JSON shape Claude Code core would
# deliver and assert exit code = 0 (allow) or 2 (block). They cover the
# Layer 4 portion of the acceptance criteria (worker boundary + org
# structure + dangerous git + no-verify + dispatcher out-of-scope).
# -----------------------------------------------------------------------
echo "# §4 hook behavior smoke"

# 4.a check-worker-boundary.sh: write inside WORKER_DIR is allowed.
HOOK="$HOOK_DIR/check-worker-boundary.sh"
stderr=$(mktmp_stderr)
json='{"tool_name":"Write","tool_input":{"file_path":"'"$WORKER_DIR_FOR_HOOKS"'/src/main.ts"}}'
ec=$(run_hook_with_env "$HOOK" "$json" "$stderr")
assert_exit 0 "$ec" "check-worker-boundary: write inside WORKER_DIR allowed"

# 4.b check-worker-boundary.sh: write outside WORKER_DIR is blocked.
stderr=$(mktmp_stderr)
json='{"tool_name":"Write","tool_input":{"file_path":"/tmp/evil.sh"}}'
ec=$(run_hook_with_env "$HOOK" "$json" "$stderr")
assert_exit 2 "$ec" "check-worker-boundary: write outside WORKER_DIR blocked"

# 4.c check-worker-boundary.sh: knowledge/raw/ kebab-case write allowed.
stderr=$(mktmp_stderr)
json='{"tool_name":"Write","tool_input":{"file_path":"'"$CLAUDE_ORG_PATH_FOR_HOOKS"'/knowledge/raw/2026-05-11-sandbox-smoke.md"}}'
ec=$(run_hook_with_env "$HOOK" "$json" "$stderr")
assert_exit 0 "$ec" "check-worker-boundary: knowledge/raw/YYYY-MM-DD-<kebab>.md allowed"

# 4.d check-worker-boundary.sh: knowledge/raw/ non-kebab write blocked.
stderr=$(mktmp_stderr)
json='{"tool_name":"Write","tool_input":{"file_path":"'"$CLAUDE_ORG_PATH_FOR_HOOKS"'/knowledge/raw/2026-05-11-Bad_File.md"}}'
ec=$(run_hook_with_env "$HOOK" "$json" "$stderr")
assert_exit 2 "$ec" "check-worker-boundary: knowledge/raw/ non-kebab filename blocked"

# 4.e block-org-structure.sh: write to <WORKER_DIR>/.claude/foo blocked.
HOOK="$HOOK_DIR/block-org-structure.sh"
stderr=$(mktmp_stderr)
json='{"tool_name":"Write","tool_input":{"file_path":"'"$WORKER_DIR_FOR_HOOKS"'/.claude/evil.json"}}'
ec=$(run_hook_with_env "$HOOK" "$json" "$stderr")
assert_exit 2 "$ec" "block-org-structure: write to <WORKER>/.claude/ blocked"

# 4.f block-org-structure.sh: write to <WORKER_DIR>/.claude/plans/foo allowed.
stderr=$(mktmp_stderr)
json='{"tool_name":"Write","tool_input":{"file_path":"'"$WORKER_DIR_FOR_HOOKS"'/.claude/plans/2026-05-11-plan.md"}}'
ec=$(run_hook_with_env "$HOOK" "$json" "$stderr")
assert_exit 0 "$ec" "block-org-structure: <WORKER>/.claude/plans/ allowed (carve-out)"

# 4.g block-no-verify.sh: 'git commit --no-verify' blocked.
HOOK="$HOOK_DIR/block-no-verify.sh"
stderr=$(mktmp_stderr)
# Indirection: substitute g_it -> git AFTER the JSON is embedded so this
# script's own source doesn't trip the outer worker's block-git-push hook
# (mirrors tests/test-block-pretooluse-hooks.sh §"substitute_run").
cmd_template='g_it commit --no-verify -m smoke'
cmd="${cmd_template//g_it/git}"
json="{\"tool_input\":{\"command\":$(printf '%s' "$cmd" | jq -Rs .)}}"
ec=$(run_hook_org_only "$HOOK" "$json" "$stderr")
assert_exit 2 "$ec" "block-no-verify: 'git commit --no-verify' blocked"

# 4.h block-no-verify.sh: 'git commit -m feat' allowed.
stderr=$(mktmp_stderr)
cmd_template='g_it commit -m feat'
cmd="${cmd_template//g_it/git}"
json="{\"tool_input\":{\"command\":$(printf '%s' "$cmd" | jq -Rs .)}}"
ec=$(run_hook_org_only "$HOOK" "$json" "$stderr")
assert_exit 0 "$ec" "block-no-verify: ordinary 'git commit -m' allowed"

# 4.i block-dangerous-git.sh: 'git reset --hard HEAD' blocked.
HOOK="$HOOK_DIR/block-dangerous-git.sh"
stderr=$(mktmp_stderr)
cmd_template='g_it reset --hard HEAD'
cmd="${cmd_template//g_it/git}"
json="{\"tool_input\":{\"command\":$(printf '%s' "$cmd" | jq -Rs .)}}"
ec=$(run_hook_org_only "$HOOK" "$json" "$stderr")
assert_exit 2 "$ec" "block-dangerous-git: 'git reset --hard HEAD' blocked"

# 4.j block-dangerous-git.sh: 'git status' allowed.
stderr=$(mktmp_stderr)
cmd_template='g_it status'
cmd="${cmd_template//g_it/git}"
json="{\"tool_input\":{\"command\":$(printf '%s' "$cmd" | jq -Rs .)}}"
ec=$(run_hook_org_only "$HOOK" "$json" "$stderr")
assert_exit 0 "$ec" "block-dangerous-git: 'git status' allowed"

# 4.k block-dispatcher-out-of-scope.sh: write to tools/ blocked.
HOOK="$HOOK_DIR/block-dispatcher-out-of-scope.sh"
stderr=$(mktmp_stderr)
json='{"tool_name":"Write","tool_input":{"file_path":"'"$CLAUDE_ORG_PATH_FOR_HOOKS"'/tools/evil.py"}}'
ec=$(run_hook_org_only "$HOOK" "$json" "$stderr")
assert_exit 2 "$ec" "block-dispatcher-out-of-scope: write to tools/ blocked"

# 4.l block-dispatcher-out-of-scope.sh: write to .dispatcher/ allowed.
stderr=$(mktmp_stderr)
json='{"tool_name":"Write","tool_input":{"file_path":"'"$CLAUDE_ORG_PATH_FOR_HOOKS"'/.dispatcher/CLAUDE.md"}}'
ec=$(run_hook_org_only "$HOOK" "$json" "$stderr")
assert_exit 0 "$ec" "block-dispatcher-out-of-scope: write to .dispatcher/ allowed"

# -----------------------------------------------------------------------
# Summary
# -----------------------------------------------------------------------
echo ""
echo "# $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
