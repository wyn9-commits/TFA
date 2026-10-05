# Project structure

Three layers, shallow, with one rule: **dependencies point inward.**

```
src/tfa_core/
│
├── domain/              Business rules. Pure Python.
│   ├── models.py            what a folio is (+ the SQL column registry)
│   ├── columns.py           single source of truth for the schema
│   ├── validation.py        consistency gates — the accuracy gate
│   ├── reconciliation.py    rate match, FX, per-night variance
│   ├── identity.py          content-addressed document_id
│   ├── rules.py             batch/filename safety (SEC-01)
│   ├── prompting.py         prompt + PROMPT_VERSION
│   ├── ports.py             protocols the adapters satisfy
│   ├── errors.py            terminal vs retryable
│   └── values.py            DocumentLayout
│
├── pipeline/            Orchestration. Talks to ports, never to vendors.
│   ├── ingest.py            FolioPipeline — one document, end to end
│   ├── extractor.py         extract → validate → repair loop
│   ├── graph_engine.py      the same algorithm as a traced state machine
│   ├── folio_rows.py        domain → SQL rows
│   └── tracing.py           facade; resolves the vendor lazily
│
├── adapters/            Everything vendor-specific. One file per thing.
│   ├── document_intelligence.py
│   ├── llm_provider.py      GPT-5.6 / Claude on Foundry
│   ├── blob_cosmos.py       Blob + processing status
│   ├── sql_engine.py        connection pool + cached Entra token
│   ├── sql_store.py         write side
│   ├── sharepoint.py        Graph delta sync
│   ├── sharepoint_locator.py  URL → ids, folder → batch name
│   ├── analyzer.py          file-type routing
│   ├── excel_reader.py, csv_reader.py
│   ├── tracing.py           LangSmith
│   └── resilience.py        retry, token bucket
│
├── config.py            typed settings
└── container.py         the ONLY place adapters are constructed
```

## Why this and not the previous layout

The old `extraction/` folder held three different kinds of thing:

| | |
|---|---|
| `validators.py`, `reconciliation.py` | pure business rules |
| `document_intelligence.py` | an Azure adapter |
| `pipeline.py` | orchestration |

Nothing said which was which, so nothing stopped them mixing. And ten files
sat at the package root — `config`, `schemas`, `identity`, `ports`, `errors`,
`values`, `db`, `resilience`, `validation_rules` — with no stated relationship
to each other.

The practical cost: **you could not import the money rules without the Azure
SDK installed.** The logic that decides a recovery claim was entangled with the
transport that fetches the document.

Now `domain/` imports nothing but the standard library and pydantic. The rate
comparison, the consistency gates and document identity can be read, reviewed
and tested on their own — which matters when the output is a figure sent to a
supplier.

## The rule is enforced, not documented

`tests/test_structure.py` fails the build on:

- any vendor SDK imported inside `domain/`
- `domain/` importing outward
- `pipeline/` importing an adapter at module level
- one adapter reaching into another's vendor
- modules left loose at the package root

A structure nothing checks is a comment. These tests are why the restructure
found three real couplings: `DocumentLayout` was being imported from an Azure
adapter despite being a plain value object; `StructuredLLM` — a protocol — lived
in the module that implements it; and `FolioPipeline` constructed its own
collaborators, so it could not be tested without credentials.

## Lazy imports are allowed, deliberately

The layering test only inspects **module-level** imports. An import inside a
function is a considered lazy resolve, and it is how an optional dependency
stays optional — `pipeline/tracing.py` resolves the LangSmith adapter at first
use, so importing the pipeline never drags in a tracing SDK.

Treating both kinds the same would push the code toward either eager coupling
or abstraction for its own sake.

## Composition root

`container.py` is the single place a concrete adapter is built:

```python
from tfa_core.container import build_pipeline
pipeline = build_pipeline()
```

Everything else receives its collaborators. That is what lets the ingestion
path run against fakes with no network, and makes swapping a model provider a
one-line change here rather than a cascade through the codebase.
