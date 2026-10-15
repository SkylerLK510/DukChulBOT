Decision: should the minute-digest notification dispatcher use a fixed pair of workers, a larger worker pool, or a staged change between them?

Context: a hosted reporting service snapshots each tenant's digest recipients simultaneously at the start of every minute. Today it serves 100 tenants; signed onboarding commitments bring that to 800 tenants in eight weeks. Each tenant has exactly 30 recipients, each requiring a separate gateway request. An existing durable outbox records the recipient snapshots and gateway receipts. The gateway supports idempotency tokens, returning the original receipt on a retry without accepting another notification. In the healthy service envelope used for this decision, each request occupies a worker and its connection for exactly 10 milliseconds, including dispatch and acknowledgement; a connection permits only one request in flight. Connections operate independently, with no shared rate limit. Up to six connections are available now. Recipient payloads remain in the outbox and can be streamed. This is a synthetic capacity case; the gateway timing is a stipulated service envelope, not an external product claim.

Hard constraints:
- With healthy gateway connections, all notifications from each minute's recipient snapshot must obtain gateway acceptance within 55 seconds of that snapshot.
- Every snapshotted recipient must obtain exactly one gateway acceptance, including across dispatcher crashes and retries.
- Resident dispatch bookkeeping must remain within 64 MiB.
