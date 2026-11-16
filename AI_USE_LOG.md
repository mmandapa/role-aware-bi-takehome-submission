# AI use log

I used ChatGPT to structure my implementation and verification instructions for Codex. I set the acceptance criteria, chose the server-side authorization boundary, removed the required confirmation step, and reviewed the evidence before sign-off. Codex GPT‑6.1 Sol handled the recorded final coding and review work. GPT‑5.5 was the application’s live planner.

The 18 entries summarize the instructions I gave Codex and the decisions or results I carried forward. They cover 11 implementation decisions and seven verification steps.

## Implementation

### 1. Define success from the assignment

> Act as a principal software engineer leading the design review for this take-home. Start with the original README, fixture, starter code, and supplied eval. Turn the brief into acceptance criteria before implementation.
>
> 1. Translate the role matrix and disclosure floors into expected behavior. Include authorized exact fields, generalized alternatives, genuine empty results, and access refusals.
> 2. Map the reporting, audit, and browser requirements to behavior a reviewer can observe.
> 3. Identify ambiguities and record the assumptions we would need to make. Preserve the original fixture and expected answers.
>
> Return a short implementation order with a check for each step. A passing access score should still require valid reports to work.

I treated access as the hard gate. Success also required correct authorized reports, a usable chart and follow-up flow, and an audit that could explain a decision. That was the standard for the starter review.

### 2. Trace what the starter lets the model control

> Now apply that checklist to the running starter as a security reviewer. Trace one report from the selected identity to the model request, SQL execution, and returned rows. Identify the first point where the server can enforce each permission rule.
>
> 1. Check who chooses the acting role and what restricts the SQL the model supplies.
> 2. Follow the result cache and conversation history keys. Explain what happens when a second identity asks the same question or reuses the conversation ID.
> 3. Trace the two supplied cases with instructions in a memo or note. Identify exactly where warehouse text becomes model input.
>
> Separate failures you can demonstrate from risks that would need another test. Use those findings to recommend the narrowest change to the report path.

The starter trusted a model-supplied role, screened SQL as text, and cached results by question. History could cross identities through a reused conversation ID. I chose to move authorization into a server-owned report path.

### 3. Replace model-authored SQL with a typed report contract

> Design the replacement report boundary as the backend engineer. The model should interpret the question and propose report fields. The server must determine the actor and decide what the proposal can access.
>
> 1. Define the permitted sources, projections, filters, grouping fields, and metrics. Use the role catalog to describe available operations.
> 2. Reject malformed arguments, caller-supplied roles, arbitrary SQL, and unsupported combinations before the database is called.
> 3. Use the same catalog when describing and compiling a report. Keep the existing entry point and eval so the changes can be tested against the original behavior.
>
> Demonstrate one valid proposal, one malformed proposal, and one well-formed proposal that the actor lacks permission to run. Show the status and SQL execution count for each.

The model now returns a typed proposal. The server resolves the actor and authorizes each requested operation. I used the three cases to check that a valid report could reach SQLite and that malformed or unauthorized proposals stopped before SQL.

### 4. Make calculations correct inside the authorized scope

> Implement the compiler from that contract. Treat the identity already resolved by the server as the source of scope. My goal is to make the query correct even when the warehouse contains inactive customers, several currencies, or multiple alerts for one transaction.
>
> 1. Apply ownership, region, and activity predicates before aggregation. Join each business record through its owning customer and keep transaction totals independent of the number of linked alerts.
> 2. Bind caller values in read-only SQLite. Use half-open UTC ranges for calendar questions.
> 3. Preserve integer minor units. Require a currency or a currency grouping when summing amounts.
>
> Compare exact rows for a regional count, a transaction total, and an authorized lookup with zero matches.

The compiler applies scope before aggregation and follows transaction ownership through joins. I checked exact values and units and kept the distinction between an empty authorized result and a refusal.

### 5. Decide what can leave the query

> The compiler now controls which rows contribute. Design the release step as a privacy engineer. I want one approved result that the server can use consistently for the answer, table, and chart.
>
> 1. Enforce the two-customer floor for customer aggregates. Check non-null contributors for averages.
> 2. For protected breakdowns, verify the ten-customer population floor and three-person cell floor. Test whether a visible total would determine a hidden cell. Refuse the breakdown when suppression alone would disclose the value.
> 3. Keep internal counts and unreleased labels local. Check that a permitted generalized attribute still works when a more precise attribute is restricted.
>
> Show examples at the boundary and state which rows, labels, or refusal may be released in each case.

The release step enforces contributor and protected-cell rules before rendering. The server uses the approved rows across the answer, table, and chart. A later test checked that an allowed protected average could still pass when it met the policy.

### 6. Carry the boundary into follow-ups and audit

> Extend the same access boundary across turns. A follow-up can refer to the previous report, but it must stay tied to the verified actor and session. Include failure and retry behavior in the design.
>
> 1. Reauthorize the saved report before repeating or charting it. Send only the report shape needed for planning. Keep saved filter values and prior rows local.
> 2. Bind turns to the actor and conversation so a different identity cannot pick up the prior report. Keep the last successful report after an error.
> 3. Audit executed SQL separately from attempts. Record scope, redacted bindings, and release counts, and commit the audit before the report is shown.
>
> Verify a reused conversation ID, a failed audit write, and a chart follow-up that uses the correct last report.

A failed request retains the last successful report. Released results wait for audit, and a chart or repeat rechecks scope. This established what the interface needed to show when a turn failed or an identity changed.

### 7. Ask for missing context before execution

> Use the report contract and follow-up state to handle incomplete questions. I want the assistant to ask the smallest useful question when the missing input changes the answer.
>
> 1. For the relative-date case, check whether the frozen fixture provides a reporting date. Ask for a date or range when the request has no anchor.
> 2. For a monetary request without a currency, keep the authorized draft and ask for the missing currency. Do not execute the incomplete aggregate.
> 3. When the user supplies the missing input, validate the revised request against the current actor and preserve the earlier approved filters.
>
> Verify the first turn’s clarification and zero-SQL audit, then verify the exact result after a valid reply.

The date clarification stops before planning when there is no anchor. An early walkthrough also found a valid currency draft being rejected and an eval check that could credit that failure as a clarification. I required regressions for both before using the flow in the final demo.

### 8. Make the browser show the distinctions reviewers need

> Build the Streamlit interface around the server behavior we can already test. A reviewer should be able to see which identity is active, what the report returned, and which query ran.
>
> 1. Let the user choose a demo identity, run a report, and ask a chart follow-up. Use the same released rows for the table and chart.
> 2. Make an authorized empty lookup visually distinct from an access refusal. Put scope and executed SQL behind Query activity.
> 3. Clear visible history and charts when identity or permissions change. Check a normal rerun for duplicate execution.
>
> Walk me through the interaction as an analyst and then a second identity. Include the evidence a reviewer should see on screen.

The browser showed scoped answers, query activity, and charts, and cleared state on identity or permission changes. I tested a preview to surface planner assumptions before execution. I then reviewed whether a separate confirmation click was warranted for complete reports.

### 9. Make the eval prove the interface still answers correctly

> Act as an evaluation engineer reviewing the initial implementation. The access gate should catch a leak, and a blanket refusal should still fail cases where the role is entitled to an answer.
>
> 1. Compare the original fixture and case expectations with the starter ZIP. Retain them and add new cases independently.
> 2. Assert exact group values and require chart data when a chart is requested. Check that authorized cases such as the reviewer note and protected averages return the permitted result.
> 3. Hold a planner request and reply constant, change the data in a temporary fixture, and check that the computed result changes.
>
> Report what was added and which checks would fail if the app returned a canned count or refused everything.

All 35 original expectations stayed in place. The added structured checks validate rows and chart data. In a separate check, moving one customer in a temporary fixture changed the count from six to seven with the same planner proposal.

### 10. Remove fixture-specific workarounds that cause false results

> Review the counterparty and narrative handling before we keep it in the submission. A valid report for a new warehouse value should follow the same rule as an existing fixture value.
>
> 1. Add a different counterparty and an unfamiliar authorized note to a temporary database. Check whether the current label or text mappings change which rows are returned.
> 2. Use literal filters and existing role and ownership checks for newly encountered values. Keep permitted narrative text local and display it as record text.
> 3. Verify that the browser can show the permitted value and that the model request and audit exclude the record text.
>
> Compare the before and after report status and result, including a request under a role that lacks access.

The allowlist could turn real counterparty totals into zero, and its completeness check could refuse valid reports. I removed both. Authorized memo and note fields are displayed as verbatim record text, with access enforced by the server. The warehouse text stays out of planner requests and the audit.

### 11. Execute complete reports directly

> Remove the mandatory confirmation step. Let the server validate scope, report shape, and required inputs, then execute a complete request on submit.
>
> 1. Ask a focused clarification when a required input is missing. Verify that this path executes zero report SQL.
> 2. Keep repeat and chart follow-ups tied to the last successful report. Recheck the actor’s permissions before reuse.
> 3. Verify that a valid request runs once, a page rerun does not execute it again, and the browser distinguishes an access refusal from an authorized empty result.
>
> Keep the audit and disclosure checks on the execution path.

I chose direct execution for complete reports after server validation. The acceptance check was one execution on submit, a reauthorized local chart follow-up, and distinct states for clarification, an empty authorized result, and a refusal.

## Verification and walkthrough

### 12. Trace one false refusal and one overly broad result

> Investigate the live runs as a principal engineer reviewing policy behavior. L2 refused a note the reviewer could read. L16 answered a request that should have been refused. Follow each request through the model proposal, the permission decision, SQL, and the outcome before deciding where to fix it.
>
> 1. Find where the authorized note was rejected and what the model needed to select the permitted linked lookup.
> 2. Check whether the model converted an exact postal filter into a broader filter. Preserve authorized ZIP3 reports while blocking the broader interpretation of an exact request.
> 3. Verify that the fix covers equivalent residence language and that the exact refusal happens before report SQL.
>
> Give me the smallest reproduced failures and the evidence that both authorized and denied paths still work after the change.

I kept the authorized linked note lookup and added a server-side check for exact postal filters. The added L18 case covers a residence request that omits the word “ZIP.” Both issues needed a check that could distinguish overly broad access from an unnecessary refusal.

### 13. Stress the boundaries and check that the tests catch breakage

> Review the implementation under failure and concurrency. Use the debugging, test-driven development, and property-testing skills I provided to check paths that the ordinary suite might miss.
>
> 1. Reproduce each confirmed defect with a failing assertion before changing the implementation.
> 2. Use barriers to force overlapping turns, cancellation, and a slow audit. Verify that an unrelated conversation can make progress while the audit is still blocked, and test recovery after reaching the active-turn limit.
> 3. Run the relevant local workload and assert that counts and audit entries match. Deliberately break scoping, suppression, or leak detection to show that the tests reject those variants.
>
> Report the workload, observed outcomes, and limits. Keep the access and correctness claims tied to what ran.

The tests reproduced interrupted SQL and blocking audit behavior. The fixes preserved execution status and moved slow audit writes outside the global conversation lock. The local load check completed 300 reports across runs with 1, 5, and 20 workers without drops or duplicates. Nine deliberately broken variants were caught.

### 14. Audit the eval and verify the planner boundary

> Before I use the scorecard, trace the original expectations, planner requests, and database results as separate sources of evidence.
>
> 1. Compare the shipped fixture and original expected answers with the starter ZIP. Check that the runtime has no dependency on eval cases or answer data.
> 2. Match the provider requests and replies to the recorded turns. For L1 and L2, verify that the authorized record is released and that its memo or note never appears in planner traffic.
> 3. Match executed SQL and released row counts to the audit. Investigate any canary match in context and retain checks on data-bearing fields.
>
> Report the evidence for each check and flag unresolved gaps before sign-off.

I used the trace review to confirm that the score came from the original fixture and expectations. L1 and L2 released the authorized memo and note, and their text stayed out of recorded planner messages. A random audit UUID could match a customer canary by chance. I kept the checks on data-bearing fields and narrowed the supplemental audit scan’s exclusion to server-generated IDs.

### 15. Explain the measured latency and cost

> Review latency as a performance engineer once correctness and access are accounted for. I want to understand what the user waits for and what changes as workload grows.
>
> 1. Report p50 and p95 across the scored live questions. Give the sample size, service failures, and whether setup turns are included.
> 2. Break out planner time from SQLite and audit work. Verify how many provider calls a chart follow-up makes before proposing any optimization.
> 3. Report input and output tokens and estimated cost with the rates and setup cost stated. Explain the first constraints at 100× the requests and at 100× the rows.
>
> Separate measured results from scaling expectations. Keep any proposed change tied to a measured bottleneck.

The earlier traces showed the planner dominated latency. The final live run measured **2.599 s p50** and **5.067 s p95** across 50 scored questions. The estimated uncached cost including setup was **$0.859240**. A chart follow-up uses the last authorized report without another provider call.

### 16. Review the branch against the original assignment

> Review the full branch against the starter ZIP and acceptance criteria. Tie each finding to a requirement and a reproducible failure.
>
> 1. Trace the public entry points for input validation, permission decisions, execution, release, and browser state.
> 2. Reproduce each confirmed discrepancy and add a regression before fixing it. Include malformed calendar values and drafts, and display precision for valid monetary results.
> 3. Verify the local suite, replay, and deliverables. Identify what still needs fresh provider calls on the reviewed runtime.
>
> Report what changed, the verification results, and the remaining limits before the walkthrough.

The review exposed three regressions. The planner catalog advertised an unsupported calendar operator, a malformed draft could escape audit, and large monetary amounts could lose a cent in display. After the fixes, **106 tests** passed and offline replay reproduced the saved outcomes. I used that checkpoint to record the walkthrough and reserved final sign-off for a fresh live run.

### 17. Record the reviewed workflow in Playwright

> Record a reviewer-facing walkthrough with Playwright. Prepare the questions, clicks, and expected results first. Use short captions so the viewer can follow the scope and query decisions without audio.
>
> 1. As the WEST analyst, show a scoped report and Query activity with the SQL visible. Ask a follow-up that charts the released rows.
> 2. Switch identities and show the cleared history. Ask for a forbidden scope and then a permitted lookup with no matches so the difference is visible. Show the currency clarification.
> 3. Use live provider calls in the final recording. Confirm that the audit matches the turns, and scan the video and logs for credentials.
>
> Deliver one short recording that shows the reviewer how reporting, access control, and audit fit together.

I requested a real-time replacement after an earlier recording used replay. The final video shows scoped SQL, the local chart follow-up, the identity switch, and refusal versus empty results. It used **six live provider replies** and wrote **seven audit entries**. The credential scan covered the logs and 76 sampled frames and found no matches.

### 18. Verify the submitted runtime with a fresh live eval

> Run the full eval against a fresh fixture using the same application and eval source that will ship. Use actual provider calls for this run.
>
> 1. Report correctness, calibration, and data access separately. Treat any access failure or incomplete provider run as a blocker.
> 2. Match returned provider replies to the per-turn traces and audit. Check the executed SQL and releases for the cases that exercise cross-identity scope and embedded instructions.
> 3. Record the exit code, scored question count, latency, token use, and estimated cost. Verify that the scorecard and notes reflect this run.
>
> I’ll sign off when the live results, inspected traces, and recorded walkthrough agree.

The November 16 run exited zero with **31/31 correctness and calibration** and **19/19 access CLEAN**. Trace verification matched 52 actual provider replies to 55 audit entries, including setup, and checked 33 SQL entries. I considered the work ready after the reviewed runtime passed the live eval, the SQL matched the execution audit, and the walkthrough demonstrated the same scope and refusal behavior. That sign-off covers the scored cases, inspected traces, and fixture workflow.
