# Event-day certifications

A strategy may trade on an EVENT_REGIME day only if its spec has `event_certified: true`
(see `src/project100c/validation/event_cert.py` and docs/research/validation.md §13.2).

That flag needs an `event_certification` record naming a `gate_result_id`. The gate result it names is saved
here as `<gate_result_id>.json` by `EventCertificationResult.save()` after
`certify_event_days(...)` passes:

- the V1-V18 gates are run on the strategy's historical **event-day** trades only;
- the verdict must be **VALIDATED**, which the toolkit gives only on **REAL** data.

The id is a hash of the file, so an edited file, a missing file, or a result for another spec version is refused
when the paper loop starts.

**No strategy is certified (2-Oct-2026).** There is no real data yet, and a SYNTHETIC run can never certify.
