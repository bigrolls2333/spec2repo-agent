# Workflow Graphs

`paper2repo_agent.graphs` contains the LangGraph workflows. Parsing, evidence,
planning, review, artifact, and security logic stays in domain modules so the
graph wiring remains explicit.

## Plan graph

`graphs/plan` creates the reviewable part of a run:

1. Load the paper input and run metadata.
2. Parse PDF, Markdown, or text into `ParsedPaper`.
3. Build evidence rows and reverse references.
4. Extract a `PaperSpec` with the configured LLM or deterministic fake mode.
5. Validate missing details, assumptions, and support level.
6. Draft a `ReproductionPlan`.
7. Build `HumanReview` and write the planning artifacts.

The CLI owns terminal prompts and approval decisions. The web console calls the
same workflow through its local job handlers.

## Execute graph

`graphs/execute` starts after an `ApprovedPlan` exists:

1. Load the approved context and stable artifact paths.
2. Build the file-generation plan and execution policy.
3. Generate the repository through templates or the optional tool-calling agent.
4. Run import, contract, smoke, and command verification.
5. Collect failures and run bounded repair steps when configured.
6. Record checkpoints, provenance, final status, and reports.

The graph supports a local SQLite checkpoint so an interrupted execution can
resume from its durable thread state.

## Public import path

The CLI imports workflows from `paper2repo_agent.graphs.plan` and
`paper2repo_agent.graphs.execute`. `paper2repo_agent.graph` remains a small
compatibility module for existing integrations and tests.
