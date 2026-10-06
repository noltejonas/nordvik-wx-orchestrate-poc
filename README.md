# 🤖 Norvik Industries — watsonx Orchestrate HR Automation PoC

> **IBM Client Engineering × Norvik Industries AB**  
> Proof of Concept: AI-Powered HR Onboarding & IT Provisioning Automation  
> Status: **Active PoC** · Sprint 2 of 3 · Target go-live: Q1 2027

---

## 🎯 Business Context

Norvik Industries AB (≈1 000 employees, HQ Stockholm) identified two high-value automation candidates during the watsonx Orchestrate discovery call:

| # | Use Case | Current Pain | Expected Gain |
|---|---|---|---|
| 1 | **HR Onboarding Agent** | Manual, 3-day cycle involving 6 teams | ≤ 4 hours end-to-end, zero manual hand-offs |
| 2 | **IT Equipment & Access Agent** | IT ticket queue; average 2-day SLA | Self-service fulfilment in < 30 min |

Both agents integrate with Norvik's existing SAP S/4HANA Cloud (Frankfurt DC), SuccessFactors Employee Central, and Azure API Management gateway — with no rip-and-replace of existing systems.

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        watsonx Orchestrate                               │
│                                                                          │
│  ┌──────────────────────┐     ┌────────────────────────────────────┐    │
│  │  HR Onboarding Agent │     │  IT Provisioning Agent             │    │
│  │  (Orchestrator LLM)  │     │  (Orchestrator LLM)                │    │
│  └──────────┬───────────┘     └──────────────┬─────────────────────┘    │
│             │                                │                           │
│  ┌──────────▼──────────────────────────────▼──────────────────────┐    │
│  │                    Skill Registry (Skills-as-a-Service)          │    │
│  │  ┌──────────────────┐  ┌────────────────┐  ┌─────────────────┐ │    │
│  │  │ SAP SuccessFactors│  │ Azure AD / Entra│  │ Email / MS Teams│ │    │
│  │  │ Skill             │  │ Skill           │  │ Notification    │ │    │
│  │  │ (OpenAPI v3)      │  │ (OpenAPI v3)    │  │ Skill           │ │    │
│  │  └──────────────────┘  └────────────────┘  └─────────────────┘ │    │
│  └────────────────────────────────────────────────────────────────┘    │
└──────────────────────────────┬──────────────────────────────────────────┘
                               │  Azure API Management (Norvik Gateway)
              ┌────────────────┼────────────────┐
              ▼                ▼                 ▼
   ┌─────────────────┐ ┌─────────────┐ ┌──────────────────┐
   │ SAP S/4HANA     │ │SuccessFactors│ │ Azure Active     │
   │ Cloud (FRA)     │ │ Employee     │ │ Directory / Entra│
   │                 │ │ Central      │ │                  │
   └─────────────────┘ └─────────────┘ └──────────────────┘
```

**Key design decisions:**
- All external calls route through Norvik's **Azure API Management** gateway — no direct cloud-to-cloud connections, satisfying their security team's requirements
- Skills are registered as **OpenAPI 3.0 specs** in watsonx Orchestrate — no custom SDK needed
- Agents use **natural language triggers** from a Teams chatbot (future phase) or the Orchestrate UI
- Data residency: SAP Frankfurt DC, Azure West Europe region

---

## 📁 Repository Structure

```
nordvik-wx-orchestrate-poc/
│
├── agents/                         # watsonx Orchestrate agent definitions
│   ├── hr_onboarding_agent.py      # HR onboarding orchestration logic
│   └── it_provisioning_agent.py    # IT access & equipment fulfilment
│
├── skills/                         # OpenAPI-backed skill implementations
│   ├── sap_successfactors_skill.py # SuccessFactors Employee Central
│   ├── azure_ad_skill.py           # Azure AD / Entra ID user provisioning
│   └── email_notification_skill.py # Teams / SMTP notification skill
│
├── config/
│   ├── agent_config.yaml           # Agent prompts, escalation rules
│   └── skill_registry.yaml         # Skill endpoints + auth config
│
├── openapi/
│   ├── successfactors_skill.yaml   # OpenAPI 3.0 spec — SAP skill
│   └── azure_ad_skill.yaml         # OpenAPI 3.0 spec — Azure AD skill
│
├── tests/
│   ├── test_hr_agent.py            # Unit tests — HR agent
│   └── test_skills.py              # Unit tests — all skills
│
├── docs/
│   ├── architecture.md             # Detailed architecture + ADRs
│   └── deployment.md               # Deployment guide (IBM Cloud)
│
├── .github/
│   └── workflows/
│       ├── ci.yml                  # Lint + test on every PR
│       └── deploy-staging.yml      # Auto-deploy to staging on merge to main
│
├── requirements.txt
├── pyproject.toml
└── .gitignore
```

---

## 🚀 Quick Start

### Prerequisites

| Tool | Version | Notes |
|---|---|---|
| Python | ≥ 3.11 | Tested on 3.11 and 3.12 |
| IBM Cloud CLI | latest | `ibmcloud login` required |
| watsonx Orchestrate CLI | latest | `wxo login` |
| Azure CLI | ≥ 2.55 | For Azure AD skill testing |

### 1. Clone & install

```bash
git clone https://github.com/noltejonas/nordvik-wx-orchestrate-poc.git
cd nordvik-wx-orchestrate-poc
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Fill in: WX_ORCHESTRATE_INSTANCE_URL, SF_API_URL, AZURE_TENANT_ID, ...
```

> ⚠️ **Never commit `.env`** — it contains secrets. Use IBM Secrets Manager or Azure Key Vault in production.

### 3. Register skills

```bash
wxo skills import openapi/successfactors_skill.yaml
wxo skills import openapi/azure_ad_skill.yaml
```

### 4. Deploy agents

```bash
wxo agents deploy config/agent_config.yaml
```

### 5. Run tests

```bash
pytest tests/ -v --tb=short
```

---

## 🧩 Agent Details

### Agent 1 — HR Onboarding Agent

**Trigger:** New hire record created in SuccessFactors  
**Goal:** Fully onboard the employee within 4 hours with zero manual steps

**Orchestration flow:**

```
1. Receive trigger (SF webhook → Azure APIM → Orchestrate)
2. Fetch employee record from SuccessFactors
3. Determine role-based access profile (config-driven mapping)
4. Create Azure AD account + assign M365 licences
5. Provision SAP ERP access (cost centre, approval workflow)
6. Send welcome email + Teams message with credentials
7. Create onboarding checklist in Teams (adaptive card)
8. Log completion back to SuccessFactors custom field
```

**Sample interaction (Orchestrate Chat):**

```
User: "Start onboarding for Anna Lindqvist, starting Monday as Senior Engineer in R&D"

Agent: "I've started Anna's onboarding. Here's what I've done:
        ✅ Fetched her SuccessFactors record (EMP-10842)
        ✅ Created Azure AD account (a.lindqvist@nordvik.se)
        ✅ Assigned M365 E3 + Copilot licence
        ✅ Provisioned SAP access — cost centre CC-RD-042
        ✅ Sent welcome email and Teams card
        ⏳ SAP ERP approval pending (auto-approves in 2h if no action)
        
        Estimated completion: 09:47 on Monday."
```

---

### Agent 2 — IT Provisioning Agent

**Trigger:** Employee self-service request via Teams chatbot  
**Goal:** Fulfil IT requests (access, equipment, software) in < 30 minutes

**Supported request types:**
- Azure AD group membership (e.g. `add me to the SAP-Viewer group`)
- Software licence provisioning (M365 add-ons, Adobe, GitHub Copilot)
- Hardware request routing (creates ServiceNow ticket with pre-filled specs)
- VPN certificate renewal
- Temporary elevated access with auto-expiry

---

## 🔐 Security & Compliance

| Control | Implementation |
|---|---|
| **Authentication** | OAuth 2.0 / Client Credentials via Azure APIM |
| **Secrets management** | IBM Secrets Manager (production), `.env` (local dev only) |
| **Data residency** | All API calls stay within EU (SAP FRA, Azure West Europe) |
| **Audit logging** | Every agent action written to IBM Log Analysis |
| **PII handling** | Employee data not persisted in Orchestrate; fetched per-request |
| **RBAC** | Skill invocation scoped to Orchestrate instance IAM roles |

> Security team sign-off: ✅ Confirmed by Sofia Hallgren (Norvik CISO team) on 2026-10-06

---

## 📊 PoC Success Metrics

| Metric | Baseline | PoC Target | Sprint 2 Result |
|---|---|---|---|
| Onboarding cycle time | 3 days | ≤ 4 hours | **2h 47min** ✅ |
| Manual hand-offs | 6 | 0 | **0** ✅ |
| IT request SLA (access) | 2 days | < 30 min | **18 min** ✅ |
| Employee satisfaction (CSAT) | 3.1 / 5 | ≥ 4.0 | Pending (go-live) |
| Error rate (skill calls) | N/A | < 2% | **0.8%** ✅ |

---

## 🗺️ Roadmap

```
Sprint 1 (done)   │ Skill scaffolding, SAP SF integration, Azure AD skill
Sprint 2 (active) │ Agent orchestration logic, end-to-end PoC testing
Sprint 3 (Q4 26)  │ Teams chatbot surface, ServiceNow integration, UAT
Go-Live (Q1 27)   │ Production deployment on IBM Cloud (eu-de)
Phase 2 (Q2 27)   │ Payroll automation, Ariba procurement agent
```

---

## 👥 Team

| Role | Name | Contact |
|---|---|---|
| IBM CE Lead | Jonas Nolte | IBM Client Engineering |
| IBM CE Architect | Sarah Chen | IBM Client Engineering |
| Norvik Project Lead | Erik Lindström | erik.lindstrom@nordvik.se |
| Norvik IT Architect | Anna Bergqvist | anna.bergqvist@nordvik.se |
| Norvik CISO | Sofia Hallgren | sofia.hallgren@nordvik.se |

---

## 📄 License

This repository is part of an **IBM Client Engineering PoC engagement** and is provided under the [Apache 2.0 License](LICENSE).

> IBM Client Engineering delivers no-charge, time-boxed innovation engagements to help clients prove business value with IBM technology. Learn more: [ibm.com/client-engineering](https://www.ibm.com/client-engineering)
