# Security model

What each gate in the write path stops, and what it does not. The family
inherits pythia's model (its SECURITY.md): the gates hold an agent that
follows the rules to them, and turn a write nobody approved into one that
takes deliberate forgery. They are not a sandbox against an agent that
sets out to forge.

| Gate | Stops | Does not stop |
|---|---|---|
| Policy (`.pythia/<engine>/policy.json`) | whole statement groups; DML, DDL and grants are `deny` by default; `allow` is refused for every group but `session` | editing the file, deliberately |
| Classification | what an engine cannot read: anonymous blocks, `CALL`, a `SELECT`, transaction control, several statements in one file, statements that cannot run in a transaction, a file that ends inside a quote — each refused with its reason | nothing it lets through is guessed at; a statement it cannot place is refused |
| Snapshot + journal | losing what a write replaced: it is on disk before the statement runs, even a statement that fails | nothing — no flag turns it off |
| Rehearsal | approving a prediction: the preview shows what the statement did, then rolls it back | effects outside the transaction while it runs: triggers fire, locks are held (bounded by `lock_timeout`), sequences advance; a function calling dblink or an FDW reaches past the rollback |
| Confirmation token | writing content, an object state or a row set that differs from what was previewed | not writing at all |
| Re-verification on confirm | a write whose verification moved after approval: something breaks now that the approved preview did not show | a breakage the person saw and approved — that lands, and exits 3 |
| Approval grant | an agent completing a write no person approved: single use, 15 minutes, bound to the connection | writing the grant file by hand, or faking a console — deliberate forgery, the `policy.json` tier |
| Console requirement | a headless agent self-approving with `approve` or `--yes`, or loosening the policy | `<PREFIX>_CI=1` for `--yes` and policy, deliberately, in a real pipeline; `approve` has no such escape |
| Read-only session | a read command that writes: the database itself refuses | dblink and foreign data wrappers |

## Honest limits

- **Phase 2 has no guard on raw clients.** pythia's PreToolUse hook denies
  writes through `sqlplus` and SQLcl; the family's guard for `psql`,
  `sqlcmd` and `mongosh` arrives with the harness in Phase 3. Until then,
  rule 5 of each engine's AGENTS.md ("never write through `psql` on
  pythia's behalf") is a rule, not a gate.
- **Approval is console-only in Phase 2.** The chat doors (a dialog card and
  an MCP approver) arrive in Phase 3. An agent cannot approve from chat
  today, and must ask the developer to run `approve` in a terminal.
- **The probe runs before approval.** An UPDATE or DELETE is approved on its
  row set, so its own table and predicate are queried before anyone
  approves. That text comes from the file about to run, so it opens no door
  the statement does not open already. pythia has the same limit.
- **A restore restores definitions and comments.** It does not restore
  rows, a dropped column or a revoked grant; `pythia-<engine> policy` prints,
  group by group, whether rollback is real.
