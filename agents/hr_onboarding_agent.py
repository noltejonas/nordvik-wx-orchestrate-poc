"""
Norvik Industries AB — HR Onboarding Agent
IBM Client Engineering PoC · watsonx Orchestrate

Orchestrates full employee onboarding across:
  - SAP SuccessFactors Employee Central (employee record)
  - Azure Active Directory / Entra ID (account + licences)
  - SAP S/4HANA Cloud (ERP access)
  - MS Teams / SMTP (notifications)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from skills.sap_successfactors_skill import SuccessFactorsSkill, EmployeeRecord
from skills.azure_ad_skill import AzureADSkill, UserProvisioningRequest
from skills.email_notification_skill import NotificationSkill, OnboardingNotification

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class OnboardingRequest:
    """Input model for an HR onboarding trigger."""
    employee_id: str
    first_name: str
    last_name: str
    role: str
    department: str
    manager_email: str
    start_date: str                        # ISO date, e.g. "2027-01-13"
    cost_centre: str
    location: str = "Stockholm"
    requested_by: Optional[str] = None


@dataclass
class OnboardingResult:
    """Aggregated result returned after full onboarding execution."""
    employee_id: str
    status: str                            # "completed" | "partial" | "failed"
    azure_account_created: bool = False
    sap_access_provisioned: bool = False
    notification_sent: bool = False
    sap_approval_pending: bool = False
    completed_at: Optional[str] = None
    errors: list[str] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)

    def add_step(self, name: str, status: str, detail: str = "") -> None:
        self.steps.append({
            "step": name,
            "status": status,
            "detail": detail,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })


# ---------------------------------------------------------------------------
# Role → access profile mapping
# ---------------------------------------------------------------------------

ROLE_ACCESS_PROFILES: dict[str, dict] = {
    "Senior Engineer": {
        "m365_licence": "E3+Copilot",
        "azure_groups": ["sg-engineers", "sg-github-users", "sg-vpn-full"],
        "sap_role": "Z_ENG_SENIOR",
        "sap_approval_required": True,
    },
    "Engineer": {
        "m365_licence": "E3",
        "azure_groups": ["sg-engineers", "sg-github-users", "sg-vpn-full"],
        "sap_role": "Z_ENG_STANDARD",
        "sap_approval_required": True,
    },
    "HR Coordinator": {
        "m365_licence": "E3",
        "azure_groups": ["sg-hr", "sg-successfactors-editors", "sg-vpn-full"],
        "sap_role": "Z_HR_COORDINATOR",
        "sap_approval_required": False,
    },
    "Finance Analyst": {
        "m365_licence": "E3+PowerBI",
        "azure_groups": ["sg-finance", "sg-powerbi-users", "sg-vpn-full"],
        "sap_role": "Z_FIN_ANALYST",
        "sap_approval_required": True,
    },
    "default": {
        "m365_licence": "E3",
        "azure_groups": ["sg-all-employees", "sg-vpn-split"],
        "sap_role": "Z_DEFAULT",
        "sap_approval_required": True,
    },
}


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class HROnboardingAgent:
    """
    watsonx Orchestrate agent that executes the full HR onboarding workflow
    for a new Norvik Industries employee.

    Designed to be invoked via:
      - watsonx Orchestrate natural language chat
      - SuccessFactors new-hire webhook (via Azure APIM → Orchestrate API)
      - Direct API call from HR systems

    Example usage::

        agent = HROnboardingAgent()
        result = await agent.run(OnboardingRequest(
            employee_id="EMP-10842",
            first_name="Anna",
            last_name="Lindqvist",
            role="Senior Engineer",
            department="R&D",
            manager_email="erik.lindstrom@nordvik.se",
            start_date="2027-01-13",
            cost_centre="CC-RD-042",
        ))
    """

    def __init__(self) -> None:
        self.sf_skill = SuccessFactorsSkill(
            base_url=os.environ["SF_API_URL"],
            client_id=os.environ["SF_CLIENT_ID"],
            client_secret=os.environ["SF_CLIENT_SECRET"],
        )
        self.azure_skill = AzureADSkill(
            tenant_id=os.environ["AZURE_TENANT_ID"],
            client_id=os.environ["AZURE_CLIENT_ID"],
            client_secret=os.environ["AZURE_CLIENT_SECRET"],
        )
        self.notify_skill = NotificationSkill(
            smtp_host=os.environ.get("SMTP_HOST", "smtp.office365.com"),
            smtp_port=int(os.environ.get("SMTP_PORT", "587")),
            smtp_user=os.environ["SMTP_USER"],
            smtp_password=os.environ["SMTP_PASSWORD"],
            teams_webhook_url=os.environ.get("TEAMS_WEBHOOK_URL"),
        )

    async def run(self, request: OnboardingRequest) -> OnboardingResult:
        """Execute full onboarding workflow. Returns aggregated OnboardingResult."""
        result = OnboardingResult(employee_id=request.employee_id, status="in_progress")
        profile = ROLE_ACCESS_PROFILES.get(request.role, ROLE_ACCESS_PROFILES["default"])

        logger.info(
            "Starting onboarding for %s %s (id=%s, role=%s)",
            request.first_name, request.last_name,
            request.employee_id, request.role,
        )

        # ── Step 1: Verify employee record in SuccessFactors ─────────────────
        try:
            emp: EmployeeRecord = await self.sf_skill.get_employee(request.employee_id)
            result.add_step("fetch_sf_record", "success",
                            f"Employee {emp.full_name} found in SuccessFactors")
            logger.info("SF record OK: %s", emp.full_name)
        except Exception as exc:
            msg = f"SuccessFactors fetch failed: {exc}"
            logger.error(msg)
            result.add_step("fetch_sf_record", "failed", msg)
            result.errors.append(msg)
            result.status = "failed"
            return result

        # ── Step 2: Create Azure AD account ──────────────────────────────────
        upn = f"{request.first_name[0].lower()}.{request.last_name.lower()}@nordvik.se"
        try:
            provision_req = UserProvisioningRequest(
                upn=upn,
                display_name=f"{request.first_name} {request.last_name}",
                department=request.department,
                job_title=request.role,
                manager_upn=request.manager_email,
                licence=profile["m365_licence"],
                groups=profile["azure_groups"],
            )
            azure_user = await self.azure_skill.create_user(provision_req)
            result.azure_account_created = True
            result.add_step("create_azure_account", "success",
                            f"Account {upn} created, licence {profile['m365_licence']} assigned")
        except Exception as exc:
            msg = f"Azure AD provisioning failed for {upn}: {exc}"
            logger.error(msg)
            result.add_step("create_azure_account", "failed", msg)
            result.errors.append(msg)

        # ── Step 3: Provision SAP access ──────────────────────────────────────
        try:
            sap_ticket = await self.sf_skill.request_sap_access(
                employee_id=request.employee_id,
                sap_role=profile["sap_role"],
                cost_centre=request.cost_centre,
                requires_approval=profile["sap_approval_required"],
            )
            result.sap_access_provisioned = True
            result.sap_approval_pending = profile["sap_approval_required"]
            result.add_step(
                "provision_sap_access",
                "success" if not profile["sap_approval_required"] else "pending",
                f"SAP role {profile['sap_role']} requested (ticket: {sap_ticket})",
            )
        except Exception as exc:
            msg = f"SAP access provisioning failed: {exc}"
            logger.error(msg)
            result.add_step("provision_sap_access", "failed", msg)
            result.errors.append(msg)

        # ── Step 4: Send welcome notification ─────────────────────────────────
        try:
            notification = OnboardingNotification(
                recipient_name=f"{request.first_name} {request.last_name}",
                recipient_email=upn,
                manager_email=request.manager_email,
                start_date=request.start_date,
                role=request.role,
                department=request.department,
                azure_account_created=result.azure_account_created,
                sap_approval_pending=result.sap_approval_pending,
            )
            await self.notify_skill.send_onboarding_welcome(notification)
            result.notification_sent = True
            result.add_step("send_welcome", "success",
                            f"Welcome email + Teams card sent to {upn} and manager")
        except Exception as exc:
            msg = f"Notification failed: {exc}"
            logger.warning(msg)   # non-fatal
            result.add_step("send_welcome", "warning", msg)
            result.errors.append(msg)

        # ── Step 5: Write completion back to SuccessFactors ───────────────────
        try:
            await self.sf_skill.update_onboarding_status(
                employee_id=request.employee_id,
                status="AUTOMATED_ONBOARDING_COMPLETE" if not result.errors else "AUTOMATED_ONBOARDING_PARTIAL",
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
            result.add_step("update_sf_status", "success", "SuccessFactors onboarding field updated")
        except Exception as exc:
            logger.warning("SF status update failed (non-fatal): %s", exc)

        # ── Finalise ──────────────────────────────────────────────────────────
        result.completed_at = datetime.now(timezone.utc).isoformat()
        result.status = "completed" if not result.errors else (
            "partial" if (result.azure_account_created or result.sap_access_provisioned)
            else "failed"
        )

        logger.info(
            "Onboarding for %s finished — status=%s, steps=%d, errors=%d",
            request.employee_id, result.status, len(result.steps), len(result.errors),
        )
        return result
