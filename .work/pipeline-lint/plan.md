# Pipeline lint remediation

Status: in progress.

Accepted owner decision: fix the listed lint findings in the current work while
preserving fail-closed behavior. This records direction only; it does not claim
implementation.

## Retained audit constraints

- Preserve justified broad exception catches at third-party/process boundaries
  when they become explicit status, warnings, or persisted errors; never
  silently discard a persistence failure.
- Q14/Q16 request TRY004 fixes, but the established `ValueError` contract for
  malformed config, JSON, and stored payloads must not change solely for lint.
  This conflict is unresolved; no code contract is changed here.
