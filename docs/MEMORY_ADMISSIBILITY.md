# Memory Admissibility

Status: draft canonical policy

## Policy Name

```text
Associative Recall Is Non-Authoritative
```

## Law Of The Land

```text
Semantic output is evidence of possible relevance, not evidence of truth.
```

## Core Invariant

```text
No memory enters active context unless admitted by deterministic governance.
```

## Retrieval Pipeline

```text
user/query/task
  ↓
STM direct continuity
  ↓
MTM current-session lookup if needed
  ↓
LTM TOC deterministic lookup
  ↓
semantic nomination only if deterministic lookup fails
  ↓
manifest/governance admission
  ↓
bounded context assembly
  ↓
audited recall event
```

## Deterministic Lookup Phase

Order:
1. STM continuity check
2. MTM targeted lookup
3. LTM TOC lookup

Requirements:
- deterministic ordering
- bounded result count
- provenance references included
- no direct context injection yet

## Semantic Nomination Phase

Allowed behavior:
- nominate candidate episodes
- provide relevance hints
- provide associative links

Forbidden behavior:
- direct context injection
- authority claims
- truth promotion without admission

## Admission Rules

A candidate memory can be admitted only when governance confirms:
1. relevance to current objective
2. provenance availability
3. policy scope compliance
4. bounded context budget compliance
5. conflict/arbitration posture is explicit when needed

## Context Injection Rules

- injection is bounded and explicit
- injected artifacts must carry source references
- unresolved conflict artifacts must be marked as conflicted
- admission decision + selected payload must be logged

## Rejection Logging

Every rejected candidate should log:
- candidate id/reference
- rejection reason code
- policy gate that denied
- timestamp and operation id

## Audit Requirements

Each recall cycle must produce an auditable trace:
- deterministic retrieval results
- semantic nominations (if any)
- admitted set
- rejected set
- final injected context slice

## Failure Modes This Prevents

- hallucinated continuity
- retrieval poisoning
- hidden affect injection
- replay nondeterminism from mutable semantic indices
- semantic authority drift
