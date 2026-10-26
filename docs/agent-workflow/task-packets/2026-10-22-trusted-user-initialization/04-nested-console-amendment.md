# Task04 nested Windows console evidence amendment (accepted)

This draft updates only the private regression evidence protocol for the two neutral Windows process tests in Task04. It does not change the CLI contract, product implementation, focus test, selected tests, accepted historical RED, or the original READY packet. Root must independently review and freeze this amendment, the new runner, its source map and its own validator before issuing any fresh stage GO. This draft does not authorize a run or claim Task04 completion.

## Actual N3 reality conflict

The previously reviewed runner `run-init-t4-sol-n1.py` (SHA-256 `d83ac34a1e1179967dd56a48f75d6630081fc640147c7282fcc689d40b3c7306`) required the neutral Job member set to equal exactly four named Python roles. In the fresh N3 regression, pytest exited 0 and the JUnit recorded 189 passed tests, yet completion `init-t4-regression-n3-completion.json` (SHA-256 `300df90f73a585bcbe25c0e5c2687b63f7ab13e778f6d882529a962beb05340f`) has `accepted=false` and `validation_error=RuntimeError`. The reports SHA-256 is `981802ac8237f8d8b36c60ce1f315c8fc71082cd3a66ea6ba89482f4f0138638`. These passing tests do not convert the rejected runner receipt into an accepted regression.

The original `neutral-lifecycle.json` receipts, original `OwnedJob` capture in the reports, and outer Job observation contain the same fifth member in each neutral case:

| Case | Inner console PID | Native image | Creation filetime | Exit | Temporal binding |
| --- | ---: | --- | ---: | ---: | --- |
| Normal `unit_neutral` | 32456 | `C:\Windows\System32\conhost.exe` | 134374710181469407 | 0 | Between launcher 134374710181232743 and parent 134374710181911139 |
| Forced `unit_forced` | 26172 | `C:\Windows\System32\conhost.exe` | 134374710202721489 | 3 | Between launcher 134374710202537774 and parent 134374710203078315 |

Each PID belongs to the exact inner `os_identity.members`, retained runtime and exit maps, ledger exits and captured original owner. The outer observer retained the same PID, image, filetime and exit. The raw receipts do not capture a console PPID, so this amendment makes no PPID claim for `conhost.exe`.

## Finite acceptance rule

For **only** `test_unit_neutral_job_and_descendant` and `test_unit_neutral_forced_tree_exit` in the regression stage, require exactly five distinct inner Job members: the same four named, distinct Python roles already validated by the READY runner, plus exactly one distinct `C:\Windows\System32\conhost.exe`. All inner runtime, retired exit and ledger exit key sets must equal those five members. The console's native image must match the fixed System32 path, its creation filetime must fall from the owner launcher through the parent runtime's creation filetime, and its exit must equal the case's designated code, 0 for normal or 3 for forced. The original case/nonce/receipt path, original owner capture and retired ledger equality, parent/descendant Python event identity and PPID chain, Python role image and time ordering, and outer Job image/filetime/exit matching remain mandatory.

This is an **exactly one** console rule for the two observed neutral cases. A fifth Python, another auxiliary, an absent/extra console, a different image, wrong time/exit, a foreign outer identity or a missing outer observation fails. It does not add an auxiliary image or exit to a global allowlist. Partial startup create/assign/resume and the ordinary pinned outer Git/conhost exit-0 rule stay as reviewed. The four outer core processes, 68 existing explicit selectors plus the complete focus module, pre-JUnit redaction, no-live-service environment, finite caps and stage-specific GO/source/phase/XML gates stay intact.

The new ignored runner is `run-init-t4-sol-n2.py`; the original n1 runner and all historical receipts remain immutable. Historical accepted RED is still bound to its original runner/receipt. A future GO for n2 must bind n2's own hash; it cannot reuse an n1 GO. The ignored `validate-init-t4-nested-sol-n2.py` is a static advisory check and is not root acceptance authority.

## Subsequent evidence gates

Root should review this narrow change against the raw N3 receipts and independently implement or adapt its validator. Then issue separate fresh GO artifacts for the complete revised focus collection/GREEN and selected non-real regression collection/execution, using new output labels. Root should compare actual node IDs and all JUnit phases, inner and outer OS evidence, source/governing hashes and clean retirement. Only newly accepted receipts can establish revised GREEN and regression; the N3 rejection remains historical evidence of the conflict.

## Acceptance record — 2026-10-26

Root accepted the exact archived execution revision after independent Astra review. Fresh N4 focus58/full regression189 passed with zero failures/errors/skips; exact OS and source evidence independently reconciled. Final Astra report SHA256 `349db8a0f2dce16ba6c3fee3062f3cf9b698bc3bc395326712b2ed9156284f77`; root aggregate `0fff78a06706c9d24fbdd06958bdadfd73981cdebe089068279e9d7968677fe1`. The earlier draft/execution text above records the historical gate; this acceptance paragraph and title update are documentary. Original exact bytes are preserved under ignored `init-t4-pre-delivery-` plus this filename. No Task05 or production acceptance is implied.
