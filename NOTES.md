# Submission notes

## Run and interface

Use **Python 3.11+** (the clean verification uses Python 3.11). The starter fixture and all 18 original correctness and 17 original access case expectations are preserved. The UI is a local demo; the identity selector is not authentication.

```sh
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python seed.py
python -m unittest discover -s tests -v
# Enter the key in your terminal with input hidden; never put it in source.
printf 'OpenAI API key: '
IFS= read -rs OPENAI_API_KEY
printf '\n'
export OPENAI_API_KEY
python eval/run_eval.py --ids c2
python eval/run_eval.py --only leaks
python eval/run_eval.py --input-usd-per-million 5 --output-usd-per-million 30
python -m streamlit run app.py --server.address 127.0.0.1
```

Pick a demo identity and submit a question. Complete reports run after backend validation. Requests missing information receive clarification. The chat shows released answers, tables, and charts. **Query activity** shows scope, filters, calculations, SQL, and released row counts on demand. Raw planner messages and private counts stay off screen. “Chart that” and “repeat that” rerun the previous report under current permissions. An identity or permission change clears the conversation.

## Scorecard and latency

The November 16 full live eval used a fresh fixture and the same app, agent, and eval source included in this submission. It recorded 55 audit entries including setup and matched 52 actual provider replies. Offline replay matched the exact planner requests and reproduced the SQL and released results. The original fixture and case expectations were preserved.

| Check | Observed result |
|---|---|
| Clean local suite | 106 tests passed. Nine deliberately broken variants were caught. |
| Final build full replay | 31/31 correctness and 19/19 access CLEAN |
| Full live eval on submission runtime | 31/31 correctness and 19/19 access CLEAN, with zero service or setup failures |
| Full live latency, 50 scored questions | p50 2.599 s, p95 5.067 s |
| Mean input/output tokens per scored question | 2,240.74 / 140.96 |
| Uncached cost estimate | $0.771625 for scored cases, $0.015433 per scored question, $0.859240 including five setup turns |

The full live scorecard is in `evidence/submission/live-full.txt`.

The eval covers the supplied fixture and added cases. New wording can still produce planning errors, and new data can change disclosure outcomes. Earlier runs exposed an age-to-birth-year substitution, an unwanted projection, an incorrect refusal, and an audit UUID matching a canary by chance. The original expected answers were preserved.

Backend timing includes audit and publication. Among 47 questions that called the provider, median planner time was 2.654 s and other backend work was 0.001 s. Cost uses $5 per million input tokens and $30 per million output tokens before cache discounts.

## Design and findings

The planner receives the question and permitted report metadata and returns a typed proposal. Warehouse results stay local. The server resolves the actor, authorizes sources/fields/predicates/grouping/metrics, adds ownership and activity predicates **before** aggregation, and binds every value. Read-only SQLite performs calculations. Python applies disclosure rules and renders the same released rows in answers, tables, and charts. Role grants are explicit; compliance cannot access protected classes and admin cannot access business rows. A person duplicated under two customer IDs counts as two customer records: no approved person key exists.

The starter trusted a model-provided role, SQL text checks, a question-only cache, and conversation IDs shared across identities. Those paths were replaced. Follow-up shapes may reach the planner. Saved filter values and report results stay local. Authorized counterparty names work directly in filters and groups. Authorized memos and notes are labeled and displayed as verbatim record text. Embedded instructions stay in that text and never reach the planner or change query scope. The app follows the assignment's field classifications and does not classify sensitive facts embedded inside prose.

A live run incorrectly refused a protected average that met the policy. I aligned the planner catalog with the permitted modes, fields, metrics, and population rules. The initial counterparty allowlist also produced false zeros for new values. I removed the allowlist and its completeness check so bound filters can match those values within scope. The validator requires an explicit birth-year basis when that measure could be mistaken for age.

A walkthrough exposed a valid monetary draft that failed without currency. The server now keeps authorized drafts and asks for the missing currency with zero report SQL. Final review also aligned calendar filters with validation, audited malformed drafts, and preserved exact cents in large integer amounts displayed in answers and tables.

Customer aggregates require two distinct contributors, including non-null metric contributors. A protected breakdown requires ten customers, reportable cells of three, and refusal when complementary suppression determines a hidden cell. Protected averages additionally require ten non-null contributors overall and three per displayed cell. Suppressed labels, values, and true totals never reach output. This version supports one protected grouping dimension; protected filters and cross-tabs are refused because their release rules are not implemented.

## Audit, limits, and deliberate omissions

`audit.jsonl` (or `BI_AUDIT_PATH`) is owner-only JSONL. Each turn records a turn ID, verified scope when available, policy version, SQL templates, redacted bindings, execution outcome, row counts, release or redaction reasons, and pre-audit timing. The server writes the audit before returning data. If the write fails, it reports the failure and retains the previous successful report. Internal audit counts stay out of the interface for denied reports. In the walkthrough, `u_ana` ran a WEST analyst lookup for a transaction and its linked alert. The audit shows two executed SQL templates, redacted lookup bindings, and one released row for each. The later company-wide request by `u_ben` records an EAST analyst refusal and zero SQL statements. The walkthrough audit is in `evidence/submission/demo.audit.jsonl`.

At 100× questions, provider quota/spend, the 64-active-turn admission cap, per-process state, and serialized audit appends become constraints. A slow audit no longer holds the global conversation-state lock, but every response still waits for its audit. Local stress covers 1/5/20 workers, overload/recovery at 64, and cancellation interleavings. At 100× fixture rows, a test verified that ownership joins preserved the exact wire total within the two-second query budget. Larger joins and higher group cardinality would put pressure on that budget. Oversized grouped results are refused, and sampled detail rows are labeled. Index tuning needs a representative workload.

The release rules apply to each report. Repeated authorized queries can still reveal information through comparison, which would require tracking disclosure across reports and identities. I left production authentication and narrative classification outside the fixture demo because they depend on trusted identity and ingestion systems. The SDK’s 25-second transport timeout and one retry leave room for longer end-to-end waits. Cancellation blocks publication while the provider call may finish. Failure recovery was tested locally.

## AI use and walkthrough

`AI_USE_LOG.md` covers 11 implementation decisions and seven verification and walkthrough steps. Each entry records the requested work and the decision or result I carried forward, including model mistakes and overrides. I stopped after the branch review, walkthrough, and passing live eval. Elapsed time against the original 4–8-hour cap is unverified.

The walkthrough in `evidence/vid.mp4` shows scoped execution, query activity, a local chart follow-up, identity switching, refusal and empty states, and clarification. It used six live GPT-5.5 replies, and the app wrote an audit for each turn.
