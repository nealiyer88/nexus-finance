# Nexus Finance — Roadmap

> This is a **priority ranking**, not a delivery schedule. Items are ordered by dependency chain and strategic value. Dates are absent by design — shipping gates are defined by success criteria, not calendar.

---

## Vision

Nexus Finance resolves entity identity across structurally incompatible financial system categories — accounting, PSA, AP, payments, CRM, payroll, expense — producing a canonical entity graph that compounds with every human approval decision. The canonical entity ID becomes the universal join key that no source system provides and no competitor produces.

The graph is the product. Connectors, agents, and FP&A features are expressions of the graph.

---

## Current Quarter Focus

**Ship V1: Cross-Category Canonical Entity Registry (QB + RUDDR)**

A deployable web app where a customer connects QuickBooks Online (accounting) and RUDDR (PSA/labor) via OAuth. Within 48 hours: canonical entity registry with every client, vendor, project, and person they've transacted with across both system categories, unified under canonical IDs. Shadow Ledger only — no write-back.

### Pre-Build Sequence (Mandatory, In Order)

1. **Canonical entity schema in SQLite** — must accommodate structurally different identifier types from different categories (QB CustomerRef vs. RUDDR project code → same canonical node). Person entity type in schema from Day 1.
2. **Cross-category ingestion prototype** — connect QB to RUDDR, prove same entity appears under structurally different identifiers, document every cross-category discrepancy pattern.
3. **Five customer interviews** — how do Controllers reconcile PSA against accounting? How do they match RUDDR project codes to QB customer records?

Read roadmap.md. Find the Active Feature Queue table. Identify the 
first feature with status QUEUED or IN PROGRESS. Read its brief 
from the path in the Brief column. Execute the pipeline steps 
from the previous orchestration prompt. When done, update the 
Status column in roadmap.md to SHIPPED.

### V1 Build — Ranked by Dependency

| Priority | Feature | Why This Order |
|----------|---------|----------------|
| 1 | Canonical entity schema + SQLite graph store | Everything depends on this. Edge tables carry category metadata. |
| 2 | QB connector (multi-tenant refactor) | Existing code, needs RLS + category tagging |
| 3 | RUDDR connector (new build) | Cross-category pair. Product thesis doesn't exist without this. |
| 4 | 6-stage matcher pipeline | Normalization → Deterministic → Blocking → Scoring → Threshold/LLM Fallback → Resolution. V1 implementations: RapidFuzz, n-gram Jaccard, graph corroboration, category-pair weight dispatch, cluster conflict detection. |
| 5 | Approval queue | Human-in-the-loop. Every approval trains the graph. Structured training data capture from Day 1. |
| 6 | Historical data pipeline + cold start seeding | Graph populated Day 2 from historical data, not empty. LLM-assisted cross-category clustering. |
| 7 | Overview dashboard (4 metric cards) | Entities resolved, auto-match rate, pending approvals, cross-category coverage. |
| 8 | Entity registry browser | Aliases grouped by system category, cross-category relationship visualization. |
| 9 | AR reconciliation module | Labor-revenue cross-reference (RUDDR hours ↔ QB invoices). First FP&A feature. |
| 10 | Connectors page + audit log | Grouped by category. Append-only, no UPDATE/DELETE. |
| 11 | Self-serve signup → OAuth flow → 48-hour delivery | The GTM motion. Shadow Ledger for first 90 days. |

---

## Top 3 Features (Post-V1, Ranked)

### 1. Bill.com Connector (AP Category)

**Why first:** Unlocks the third system category. QB (accounting) + RUDDR (PSA) + Bill.com (AP) = three-category identity graph. Demonstrates the within-customer network effect — each additional category multiplies resolution surface. Also unlocks "integration readiness" GTM motion.

**Dependency:** V1 shipped, ≥3 customers sustaining ≥90% auto-match across QB + RUDDR for 3 consecutive cycles.

### 2. Stripe Connector (Payments Category)

**Why second:** Clean API, low complexity. Fourth category. Payment-to-invoice matching closes the cash application loop. Enables cash flow forecasting prerequisite.

**Dependency:** Bill.com connector shipped. Cross-category coverage metric ≥80% across 3 categories.

### 3. Per-Person Profitability (Requires Gusto Connector)

**Why third:** Most valuable query for professional services: fully-loaded profitability per person per engagement. Requires Gusto (payroll) connector for cost rates. Person entity schema exists in V1, but profitability calculation requires cost data from payroll category.

**Dependency:** PII security controls proven and audited. Gusto connector carries SSNs, salary, tax records — security failure is company-ending. Build ONLY after security audit passes.

---

## Kill Criteria — What We Are NOT Building

These are permanently out of V1 scope. Some are V2+; some are never.

| Kill Item | Why |
|-----------|-----|
| Neo4j or any graph database | SQLite with edge tables. Complexity unjustified until >50K nodes. |
| Vector embeddings / semantic search | RapidFuzz + n-gram Jaccard + graph corroboration. fastText is V2+. |
| XGBoost scorer | Deterministic category-pair weight dispatch. XGBoost replaces fixed weights in V2+. |
| Self-hosted LLM | Claude API for Tier 3 fallback only (<15%). Self-hosted is V2+. |
| Agent orchestration framework | Sequential Python functions. No LangChain, no CrewAI, no AutoGen. |
| Any connector beyond QB + RUDDR | Bill.com is first post-V1 expansion. |
| Snowflake / Databricks integration | After 10+ customers. Distribution channel, not V1 feature. |
| Write-back to source systems | Shadow Ledger only. Request write permissions after 90 days. |
| Forecasting module | Phase 3 of FP&A roadmap. Requires Phase 2 (AR/AP recon) proven. |
| Multi-region deployment | Not until enterprise tier exists. |
| FedRAMP / NIST certification | Premium tier. Not V1. |
| Intra-category deduplication (QB + Xero) | Not the product thesis. Two accounting systems prove nothing. |
| Merge.dev or any universal API abstraction | Destroys dimensional richness required for category-aware matching. Build connectors natively. |
| Freemium tier | Price qualifies pain. |

---

## FP&A Feature Phases (Post-V1, Sequential)

Each phase depends on prior phase proven in production. No skipping.

| Phase | Feature | Success Gate |
|-------|---------|-------------|
| 1 | Cross-Category Entity Resolution | ≥90% auto-match, 3 customers, 3 cycles, across both categories |
| 2 | AR/AP Reconciliation | ≥98% accuracy, 3 customers, 2 cycles |
| 3 | Cash Flow Forecasting | Per-entity payment cadence + labor-revenue lag. Rolling 13-week model. |
| 4 | P&L Intelligence | Cross-category variance attribution |
| 5 | Anomaly Detection | Revenue recognition gaps, labor-revenue mismatch, payment drift, confidence decay |

---

## Model Evolution Path

| Stage | Customers | Implementation | Cost |
|-------|-----------|---------------|------|
| 1 | 0–5 | Claude API few-shot, Tier 3 fallback (<15%) | ~$5–20/ingestion run |
| 2 | 5–20 | Self-hosted open model (Nemotron Nano or equivalent), parallel assessment (~45–60%) | GPU cost, near-zero marginal per call |
| 3 | 20+ | Fine-tuned on accumulated approval pairs with structured reasoning traces | Proprietary asset |

**Load-bearing decisions (permanent):** Training data pipeline, structured output format, data sharing rights in ToS from Day 1.
**Reversible decisions:** Specific base model (Nemotron, Llama, Mistral — whatever is best at deployment).

---

## Distribution Sequencing

1. Network-first (direct outreach to Controllers at professional services firms)
2. QB App Store listing
3. RUDDR / Harvest integration listing
4. Snowflake Marketplace (after 10 customers)
5. Databricks Partner Accelerator (after 20 customers)
6. Microsoft Fabric / Azure Marketplace (gated behind Dynamics 365 connector)

**Marketplace listings amplify credibility, don't generate demand.** Partner relationships are the real bottleneck.
