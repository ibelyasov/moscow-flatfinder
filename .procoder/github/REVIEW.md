# Pre-PR review rubric

Review the full branch diff before publishing. Findings name the file and line, what breaks, and the smallest safe fix.

- Validate user-supplied strings at trust boundaries.
- Preserve honest error handling and state consistency.
- Avoid repeated per-iteration work.
- Keep temporary files private and unpredictable.
- Wire new behavior through code, documentation, and runnable checks.
- Treat parser inputs as hostile.
- Keep prose and Markdown accurate and readable.

End with a verdict line: findings counted by severity, or exactly "Nothing found — open the PR."
