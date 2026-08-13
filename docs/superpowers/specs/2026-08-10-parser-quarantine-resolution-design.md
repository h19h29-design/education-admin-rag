# Parser Quarantine Resolution Design

## Outcome

Provide a human-reviewed, value-free authority that accounts for every parser
quarantine occurrence exactly once and can drive a deterministic reparse. The
existing v3 review package remains non-ready. A new review package may be
staged only after the verified authority has no unresolved entries, the
closed year dispatcher reparses the original bound pages, and the resulting
parse has zero parser quarantines.

## Authority boundary

`parser-quarantine-resolutions.json` is canonical ASCII JSON. It binds the
release, registry, manifest, raw, parser, and original quarantine SHA-256
authorities and count. Repeated identical quarantine rows receive distinct
occurrence ordinals and occurrence IDs. Every entry preserves document, year,
location, pages, source spans, bboxes, and text hashes without source text.

Allowed dispositions are `unresolved`, `confirmed_noncase`, and `corrected`.
An upstream extraction failure can only remain `unresolved`. A corrected entry
contains role annotations that reference exact existing source spans; values
are recovered from those spans during reparse and never supplied by the
sidecar. Each disposition change is represented by a canonical hash-chained
event envelope with a broker-derived actor ID.

The creator writes an owner-only sidecar and reports its SHA-256. Verification
is a separate operation and requires that SHA-256 as an external argument.
The loader uses bounded, no-follow, regular-file reads and rejects noncanonical
bytes, extra fields, incomplete coverage, duplicate occurrence IDs, broken
event chains, and any authority mismatch.

## Deterministic reparse

The apply path accepts only validated original `ParserPage` objects and uses a
closed dispatch for 2020 through 2025. `confirmed_noncase` removes only the
exact reviewed spans covered by its occurrence. `corrected` changes only the
semantic role of exact reviewed spans through a narrow verified annotation
projection. Hierarchy and case values are derived from the matched parser
lines. Unreferenced spans are unchanged. The dispatcher rejects missing or
extra pages, overlapping contradictory annotations, unresolved entries, and
upstream failures.

The output is accepted only when all original occurrences were consumed once
and the reparsed result has no quarantine. It remains `needs_review`; no case
is search- or answer-approved by this workflow.

## Operational boundary

The review broker must derive actor identity from the peer credential and emit
the event envelope. Until that broker operation is wired and exercised on the
NAS, sidecars produced by a direct wrapper are test/development artifacts and
must not be treated as human-ready. Restaging always targets a new empty
release directory. Finalization continues to reject the original v3 package
and all unbound artifacts.

