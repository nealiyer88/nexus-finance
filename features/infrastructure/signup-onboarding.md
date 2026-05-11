# Feature Brief: Self-Serve Signup + OAuth Onboarding Flow

**Author:** Neal Iyer
**Date:** 2026-05-10
**Status:** Approved
**Complexity:** L
**FP&A Phase:** Infrastructure

---

## Problem Statement

The GTM motion requires self-serve: a customer signs up, connects QB via OAuth, connects RUDDR via API key, and receives their cross-category entity registry within 48 hours. Without this flow, every customer requires manual onboarding. This is the last V1 feature — it connects all prior features into a deployable product.

---

## Scope

### In Scope

- Create `api/routers/auth.py`:
  - `POST /auth/signup` — create tenant, provision Supabase user
  - `POST /auth/login` — JWT token issuance via Supabase Auth
  - `GET /auth/callback/quickbooks` — OAuth2 callback, store encrypted tokens
  - `GET /auth/callback/ruddr` — API key validation and storage

- Create onboarding wizard (Dash multi-step page):
  - Step 1: Sign up (email, password, company name)
  - Step 2: Connect QuickBooks (OAuth redirect)
  - Step 3: Connect RUDDR (API key input)
  - Step 4: Confirm connections, trigger historical data pull
  - Step 5: "Processing — your entity registry will be ready within 48 hours" status page

- Create `workers/ingestion_worker.py`:
  - Background job that runs historical seeding pipeline (feature 13) after onboarding
  - Status tracking: queued → processing → complete → ready for review
  - Error handling: retry on transient failures, alert on permanent failures

- Stripe integration for billing:
  - Create customer on signup
  - Attach Starter plan ($500/mo) subscription
  - Webhook handler for payment events

- **Test suite:** `tests/test_onboarding.py`
  - Assert: signup creates tenant and Supabase user
  - Assert: QB OAuth callback stores encrypted tokens
  - Assert: RUDDR API key validated and stored
  - Assert: ingestion worker triggered after both connections confirmed
  - Assert: Stripe customer created with correct plan

### Out of Scope

- SSO / SAML authentication — enterprise tier
- Team member invitations — V2
- Plan upgrades (Starter → Professional) — V2
- Custom domain / white-labeling
- RUDDR OAuth (they use API keys, not OAuth)

---

## Success Criteria

- [ ] Complete signup → connect QB → connect RUDDR → processing flow works end-to-end
- [ ] OAuth tokens stored encrypted per tenant
- [ ] RUDDR API key stored encrypted per tenant
- [ ] Ingestion worker triggered automatically after both connections confirmed
- [ ] Status page shows processing progress
- [ ] Stripe customer and subscription created on signup
- [ ] Shadow Ledger enforced: no write permissions requested during onboarding
- [ ] `pytest tests/test_onboarding.py` passes

---

## Dependencies

- [ ] ALL prior features shipped — this is the integration point
- [ ] Supabase Auth configured with OAuth providers
- [ ] Stripe account with Starter plan product created
- [ ] Railway.app deployment configured
- [ ] Resend configured for transactional emails (welcome, processing complete)

---

## Estimated Complexity

**Rating:** L

**Rationale:** OAuth2 flow, Stripe integration, background worker, multi-step wizard, deployment configuration. Touches every layer of the stack. Most integration-heavy feature in V1.

---

## PROJECT CONTEXT

### Onboarding Timeline (from spec)

- Minute 0: Signup
- Minute 5: QB connected via OAuth
- Minute 10: RUDDR connected via API key
- Hour 1–48: Historical data pulled, entities clustered, graph seeded
- Hour 48: Email notification — "Your entity registry is ready. 23 name variants across QB and RUDDR for 8 unique clients, unified for the first time."

### Shadow Ledger Enforcement

- No write permissions requested at signup
- All connections read-only for first 90 days
- Write permissions requested after 90 days of proven accuracy

### Pricing

- Starter: $500/month — 2 system categories, up to 500 entities
- Professional: $1,500/month — 5 categories, unlimited entities, forecasting

### Relevant Spec Sections

- Section 15: Go-to-Market Strategy (Shadow Ledger motion, pricing)
- Section 4: V1 Product Definition (self-serve signup)
