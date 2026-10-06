# Detailed Architecture — Norvik watsonx Orchestrate PoC
# IBM Client Engineering

## System Context

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           Norvik Industries AB                               │
│                                                                              │
│  ┌─────────────────────────────────────────────────────────────────────┐    │
│  │                    MS Teams (Employee Surface)                        │    │
│  │  New hire event ──► HR Onboarding Agent  │  IT request ──► IT Agent  │    │
│  └───────────────────────────┬─────────────────────────┬───────────────┘    │
│                              │ watsonx Orchestrate API  │                    │
│  ┌───────────────────────────▼─────────────────────────▼───────────────┐    │
│  │                   IBM Cloud (Frankfurt, eu-de)                        │    │
│  │                   watsonx Orchestrate Instance                        │    │
│  │  ┌──────────────────────────────────────────────────────────────┐   │    │
│  │  │  Skill Registry (OpenAPI 3.0)                                 │   │    │
│  │  │  - successfactors_skill.yaml                                  │   │    │
│  │  │  - azure_ad_skill.yaml                                        │   │    │
│  │  │  - email_notification_skill (Python function)                 │   │    │
│  │  └──────────────────────────────────────────────────────────────┘   │    │
│  └───────────────────────────┬─────────────────────────────────────────┘    │
│                              │ HTTPS / OAuth 2.0                             │
│  ┌───────────────────────────▼─────────────────────────────────────────┐    │
│  │              Azure API Management (apim.nordvik.se)                  │    │
│  │  - OAuth 2.0 token exchange                                          │    │
│  │  - Rate limiting (1000 req/min per skill)                            │    │
│  │  - Request logging → Azure Monitor                                   │    │
│  │  - EU data residency enforcement (policy: deny non-EU backends)      │    │
│  └──┬────────────────────────────┬───────────────────────┬────────────┘    │
│     │                            │                       │                   │
│  ┌──▼──────────┐  ┌──────────────▼────────┐  ┌──────────▼──────────────┐   │
│  │ SAP SF EC   │  │ MS Graph API          │  │ SAP S/4HANA Cloud       │   │
│  │ (Frankfurt) │  │ (Azure West Europe)   │  │ (Frankfurt DC)          │   │
│  └─────────────┘  └───────────────────────┘  └─────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Architecture Decision Records

### ADR-001: Route all skills through Azure APIM

**Status:** Accepted  
**Date:** 2026-09-15  
**Context:** Norvik's security team requires all outbound API calls from IBM Cloud to traverse their existing Azure API Management gateway for logging, rate limiting, and data residency enforcement.  
**Decision:** All watsonx Orchestrate skill endpoints point to `apim.nordvik.se` rather than directly to SAP or MS Graph. APIM performs OAuth token exchange and proxies the request.  
**Consequences:** +Security approval gained. +Audit log in one place. -2 additional network hops (~15ms latency per call, acceptable for async workflows).

---

### ADR-002: Use IBM Granite 3 as the orchestration LLM

**Status:** Accepted  
**Date:** 2026-09-22  
**Context:** Norvik requires all AI processing to stay within IBM Cloud eu-de (Frankfurt) region for GDPR compliance.  
**Decision:** Use `ibm/granite-3-3-8b-instruct` as the agent LLM in watsonx Orchestrate. No external model APIs (OpenAI, Azure OpenAI) are used.  
**Consequences:** +Full data residency in IBM Cloud eu-de. +No data leaves the agreed cloud boundary. -Slightly lower benchmark scores than GPT-4o on complex reasoning tasks, mitigated by structured skill definitions.

---

### ADR-003: Fail-forward, not fail-fast, in the onboarding agent

**Status:** Accepted  
**Date:** 2026-09-28  
**Context:** Employee onboarding involves 5 independent steps. A failure in one (e.g., SAP access provisioning) should not block the others (Azure AD creation, welcome email).  
**Decision:** The HR Onboarding Agent collects errors but continues executing remaining steps. Final status is `completed`, `partial`, or `failed` based on how many critical steps succeeded.  
**Consequences:** +Resilient UX — employee gets their Azure account even if SAP is down. +HR team receives partial completion reports. -Slightly more complex result handling.

---

### ADR-004: Temporary elevated access capped at 8 hours

**Status:** Accepted  
**Date:** 2026-10-01  
**Context:** Norvik's security policy requires time-limited access for all privileged resources.  
**Decision:** `TEMP_ELEVATED_ACCESS` requests are hard-capped at 8 hours in the IT Provisioning Agent. Requests for longer durations silently cap at 8h and notify the requester.  
**Consequences:** +Aligns with Norvik CISO team requirements (confirmed by Sofia Hallgren). +Reduces standing privilege risk. -Users with legitimate long-running needs must re-request.

---

## Data Flow — HR Onboarding (happy path)

```
SuccessFactors   Azure APIM    Orchestrate    Azure AD    Email/Teams
     │                │             │             │            │
     │  Webhook: new  │             │             │            │
     │  hire event    │             │             │            │
     ├───────────────►│             │             │            │
     │                │  Trigger    │             │            │
     │                │  agent run  │             │            │
     │                ├────────────►│             │            │
     │   GET /employees/{id}        │             │            │
     │◄────────────────────────────┤             │            │
     ├────────────────────────────►│             │            │
     │                             │  POST /users│            │
     │                             ├────────────►│            │
     │                             │◄────────────┤            │
     │                             │  Assign lic │            │
     │                             ├────────────►│            │
     │  POST /sap-access-requests  │             │            │
     │◄────────────────────────────┤             │            │
     ├────────────────────────────►│             │            │
     │                             │             │  Welcome   │
     │                             │             │  email + card
     │                             ├─────────────────────────►│
     │  PATCH /employees/{id}/     │             │            │
     │  onboarding (status=DONE)   │             │            │
     │◄────────────────────────────┤             │            │
     ├────────────────────────────►│             │            │
     │                             │  Result:    │            │
     │                             │  completed  │            │
```
