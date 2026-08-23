# Workflow rules

Repo-level rules the procoder skills read and follow. What is written here wins over the skills' built-in defaults.

## Worktrees

Feature work happens in one worktree per branch. Use the harness's native worktree support when available.

## After a successful merge

Delete the remote and local feature branch and fetch with pruning. Detach a harness-managed worktree before deleting its branch; never remove a harness-managed worktree.
