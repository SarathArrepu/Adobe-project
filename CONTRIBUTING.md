# Contributing Guide

## Git Workflow Rules

These three rules apply to every branch and every contributor. They are enforced by the git hooks in `.githooks/` (see [Setup](#setup) to activate them).

---

### Rule 1 — Always pull before committing

Before making new commits on any branch, your local branch must be in sync with its remote tracking branch.

```bash
git pull --rebase origin <your-branch>
# then stage and commit
```

**Why:** Committing on top of a stale base creates unnecessary divergence and makes conflicts larger and harder to resolve.

**Hook:** `pre-commit` fetches from origin and blocks the commit if your local branch is behind its remote tracking branch.

---

### Rule 2 — Keep feature branches in sync with main

Rebase your feature branch onto `origin/main` regularly — at minimum before opening a PR and whenever `main` has received significant changes.

```bash
git fetch origin
git rebase origin/main
# fix any conflicts, then:
git rebase --continue
git push --force-with-lease origin <your-branch>
```

**Why:** Long-lived branches that drift far from `main` accumulate large merge conflicts that are time-consuming and error-prone to resolve.

**Hook:** `pre-commit` warns (but does not block) when your branch is more than 10 commits behind `origin/main`. `post-merge` lists any local branches that are more than 5 commits behind after a pull to `main`.

---

### Rule 3 — Check for merge conflicts before pushing

Before pushing, verify that your branch has no conflicts with `origin/main`. If the pre-push hook detects conflicts it will block the push and tell you exactly how to resolve them.

```bash
# To resolve a conflict flagged by the hook:
git fetch origin
git rebase origin/main      # triggers conflict markers in affected files
# edit files to resolve, then:
git add <resolved-files>
git rebase --continue
git push --force-with-lease origin <your-branch>
```

**Why:** Catching conflicts locally before they reach the PR keeps the CI green and saves review time. `--force-with-lease` is safe because it only force-pushes if the remote hasn't been updated by someone else since your last fetch.

---

## Setup

The hooks live in `.githooks/` and are tracked in the repository, so every clone gets them. Activate them once per clone:

```bash
git config core.hooksPath .githooks
```

To verify the hooks are active:

```bash
git config core.hooksPath    # should print: .githooks
ls .githooks/                # should list: pre-commit, pre-push, post-merge
```

---

## Standard Branch + PR Workflow

```
1. Start from a fresh main
   git checkout main && git pull --rebase origin main

2. Create a feature branch
   git checkout -b feature/my-change

3. Make changes, commit (hook checks you're not stale)
   git add <files>
   git commit -m "feat: description"

4. Stay in sync while working
   git fetch origin && git rebase origin/main

5. Push (hook checks for conflicts with main before allowing)
   git push -u origin feature/my-change

6. Open a PR
   gh pr create --title "..." --body "..."

7. After CI passes and PR is approved → merge via GitHub UI

8. Clean up
   git checkout main && git pull --rebase origin main
   git branch -d feature/my-change
```

---

## Hook Reference

| Hook | Trigger | Rule enforced |
|---|---|---|
| `pre-commit` | Before every commit | Branch not behind remote tracking branch (blocks); branch >10 commits behind main (warns) |
| `pre-push` | Before every push | No merge conflicts with `origin/main` (blocks if conflicts found) |
| `post-merge` | After every pull/merge to main | Lists local branches that are >5 commits behind main (informational) |
