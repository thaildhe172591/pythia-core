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
| Prediction (`predicted`, the predict tier) | approving a change that cannot be rehearsed blind: the preview predicts it from the data as it stands, and says "predicted — nothing ran"; a prediction that moved since approval refuses the write; the write runs once, after approval; what landed is read back, and anything unlike the prediction exits 3 with the restore; `deploy` never holds one | what happens between the check and the write — another writer, documents arriving — is reported after the write, not prevented: MongoDB holds no transaction around these commands |
| Escape report (`escaped`) | a statement whose transaction ended inside it — a SQL Server trigger's `COMMIT` — being called unwritten: it exits 3 with the restore, its approval is spent, a lease pauses, and a release names every statement up to it | the write itself: what a trigger committed is committed, approved or not. The engine makes it hard (pythia-mssql rehearses one transaction level down, so a single `COMMIT` commits nothing) and core makes it loud |
| Confirmation token | writing content, an object state or a row set that differs from what was previewed | not writing at all |
| Re-verification on confirm | a write whose verification moved after approval: something breaks now that the approved preview did not show | a breakage the person saw and approved — that lands, and exits 3 |
| Approval grant | an agent completing a write no person approved: single use, 15 minutes, bound to the connection | writing the grant file by hand, or faking a console — deliberate forgery, the `policy.json` tier |
| Console requirement | a headless agent self-approving with `approve` or `--yes`, or loosening the policy | `<PREFIX>_CI=1` for `--yes` and policy, deliberately, in a real pipeline; `approve` has no such escape |
| Chat door (AskUserQuestion + `approve --hook`) | a grant the developer did not choose: the agent's words instead of the card, an answer the agent wrote into its own call, a dialog that closed unattended, a typed reply, no answer; an agent piping a payload into `approve --hook` (the guard and a deny rule) | a client setting that answers AskUserQuestion itself (`doctor` names `dontAsk`, an allow rule, a `*` matcher); a person who approves without reading |
| Release (`deploy`) | landing part of a release that was not approved as a whole: a statement edited after approval, a member whose object, rows or verification moved, a failure halfway — each rolls the whole release back; a member's own token cannot be approved alone | effects outside the transaction while the release runs (the rehearsal's limits, for longer: every lock is held until the release commits or rolls back) |
| Autopilot lease | a write landed unasked that the lease does not cover: another or a protected connection, a group or a destructive statement the card did not name, more than 1000 rows, a lease expired, spent, revoked, paused or never approved; a lease file written by hand (the guard) | what the developer approved: inside its scope, writes land without a per-write look — each is still journaled, verified and linted (a lint error is never landed under a lease), and breakage pauses the lease |
| MCP door (elicitation) | a grant without an explicit Approve: a decline, a cancel, a client that declines for nobody in under 1.5 s, a prompt that could not be shown | a client that shows the prompt and accepts it without the person |
| Read-only session | a read command that writes: the database itself refuses | dblink and foreign data wrappers |
| PreToolUse guard (`hook pre-tool`) | an agent that follows its rules into a known way around the gate: the raw client given write SQL or a script — named anywhere in the command, so also behind `env`, `sudo`, `docker exec`, `ssh`, a pipe or `bash -c`, an MCP SQL tool given write SQL, a grant written by hand, `grants.mint` or the console seam from a one-liner, the agent setting `<PREFIX>_CI`, reading `connections.json`, editing `policy.json` or the journal | a shell spelling no pattern lists (a variable, an alias, a script file that runs the client); the guard is a hook in the agent's client, and an agent with no hook wired has no guard — `doctor` says so |
| Deny rules (`install`) | Claude Code's own tools reading `connections.json` or editing the engine's folder, even with no hook running | the shell, which the rules do not cover |

## Honest limits

- **The guard raises the bar; it is not a wall.** It reads a shell command
  for what it names, never for what it means, so it denies some harmless
  commands (a `grep` for `psql -f` in quotes) and cannot see a write spelled
  in a way it does not list. Behind it, the gate's own checks — the token,
  the grant, the re-rehearsal — hold whatever the hook decided. Writing a
  grant through a spelling the guard misses is still deliberate forgery,
  the `policy.json` tier.
- **The guard reads what the agent's client hands it.** It runs where
  `install` wired it (Claude Code). An agent whose client runs no hooks is
  held by the instruction block install writes into AGENTS.md and by the
  gate's own checks, not by the guard.
- **A chat door trusts the client that draws it.** The hook reads the
  answer Claude Code hands it, and the elicitation the answer Codex returns.
  A client that answers for the person defeats both; the terminal door is
  the one that needs no client at all.
- **The probe runs before approval.** An UPDATE or DELETE is approved on its
  row set, so its own table and predicate are queried before anyone
  approves. That text comes from the file about to run, so it opens no door
  the statement does not open already. pythia has the same limit.
- **A restore restores definitions and comments.** It does not restore
  rows, a dropped column or a revoked grant; `pythia-<engine> policy` prints,
  group by group, whether rollback is real.
