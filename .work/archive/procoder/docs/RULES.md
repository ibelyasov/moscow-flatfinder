# Documentation rules

MoscowFlatFinder keeps user-facing setup in `README.md` and product decisions in
the Russian-language documents under `docs/`.

## Required docs

- README.md

## Required badges

No badges are required for this experimental public project.

## README first screen

No additional first-screen sections are required.

## Version-tracked docs

- README.md

## README must mention

No additional feature keywords are required.

## Guidance

- Keep runtime commands copy-pasteable and aligned with the repository checks.
- Keep user-facing prose in Russian even when a document uses an English file
  name, and keep relative Markdown links valid.
- Keep operational steps in `README.md`; explain product constraints and
  scoring decisions in the relevant document under `docs/`.
- Verify relative links and render Mermaid diagrams before publication.

## Audit decisions

- Add docstrings to actual external contracts or non-obvious behavior, not to
  every non-underscored symbol in this private application.
- Refactor high-complexity functions only around a concrete behavior change
  with a focused smoke check; complexity alone does not justify churn.
- Preserve broad exception handling at third-party and process boundaries where
  FlatFinder converts failures into explicit status, warnings, or persisted
  errors. Never silently discard a persistence failure.
- Preserve the established `ValueError` validation contract for malformed
  configuration, JSON, and stored payloads. Changing it to `TypeError` is an API
  change, not a lint cleanup.
- Keep nested conditions when they make separate trust checks visible; do not
  collapse them solely to satisfy `SIM102`.
