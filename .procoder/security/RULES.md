# Security rules

## Blocking lines

- Detected secrets always block and require removal plus rotation.
- SAST findings at ERROR severity block until fixed or reviewed as a documented,
  line-specific false positive.
- Dependency vulnerabilities with CVSS 7.0 or above block.

## False positives

Suppress only the exact reviewed line using the scanner's native mechanism and
state why the reported data flow is safe. Never weaken or disable a repository
scanner globally.

## Dynamic SQLite identifiers

The demo-database sanitizer may inspect tables and columns from an untrusted
SQLite schema. Quote every discovered identifier with `_quote_identifier` and
continue to bind all data values as parameters. A scanner suppression is valid
only on the reviewed execution boundary and must stay covered by the malicious-
identifier smoke check.

## Never

- Never write credential values to logs, reports, commits, or chat.
- Never concatenate untrusted values into SQL, shell commands, paths, or URLs.
- Never treat a missing scanner as a clean result.
