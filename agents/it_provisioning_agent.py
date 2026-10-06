"""
Norvik Industries AB — IT Provisioning Agent
IBM Client Engineering PoC · watsonx Orchestrate

Self-service IT fulfilment agent invoked via Microsoft Teams chatbot.
Handles: Azure AD group access, M365 licence changes, hardware requests,
VPN certificates, and temporary elevated access with auto-expiry.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional

from skills.azure_ad_skill import AzureADSkill
from skills.email_notification_skill import NotificationSkill

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums & models
# ---------------------------------------------------------------------------

class RequestType(str, Enum):
    GROUP_ACCESS = "group_access"
    LICENCE_CHANGE = "licence_change"
    HARDWARE_REQUEST = "hardware_request"
    VPN_CERTIFICATE = "vpn_certificate"
    TEMP_ELEVATED_ACCESS = "temp_elevated_access"


@dataclass
class ITRequest:
    """Incoming IT provisioning request from Teams chatbot or Orchestrate chat."""
    requester_upn: str
    request_type: RequestType
    details: dict
    justification: Optional[str] = None
    manager_approval_required: bool = False


@dataclass
class ITProvisioningResult:
    """Result of an IT provisioning request."""
    request_id: str
    requester_upn: str
    request_type: RequestType
    status: str                            # "fulfilled" | "pending_approval" | "rejected" | "failed"
    fulfilled_items: list[str] = field(default_factory=list)
    pending_items: list[str] = field(default_factory=list)
    sla_minutes: Optional[int] = None
    expires_at: Optional[str] = None       # For temp access
    ticket_id: Optional[str] = None        # ServiceNow ticket for hardware
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Policy: which groups require manager approval
# ---------------------------------------------------------------------------

HIGH_PRIVILEGE_GROUPS = {
    "sg-sap-admin",
    "sg-azure-subscription-owner",
    "sg-infra-prod",
    "sg-secrets-access",
    "sg-hr-data-full",
    "sg-finance-data-full",
}

# Licences that can be self-served vs require approval
SELF_SERVE_LICENCES = {"E3", "E3+Copilot", "PowerBI-Pro", "Visio-Plan1"}
APPROVAL_REQUIRED_LICENCES = {"E5", "Azure-DevOps-Enterprise", "GitHub-Copilot-Enterprise"}

TEMP_ACCESS_MAX_HOURS = 8
TEMP_ACCESS_DEFAULT_HOURS = 4


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class ITProvisioningAgent:
    """
    watsonx Orchestrate agent for self-service IT requests at Norvik Industries.

    Invoked from:
      - Microsoft Teams adaptive card (primary surface)
      - watsonx Orchestrate chat
      - Automation flows (e.g., scheduled VPN cert renewals)

    Example (natural language via Teams)::

        User: "Add me to the SAP-Viewer group"
        Agent: "✅ Done! Added you to sg-sap-viewer.
                Access is effective within 5 minutes."

        User: "I need admin access to the production subscription for a hotfix"
        Agent: "That group requires manager approval. I've sent a request to
                erik.lindstrom@nordvik.se — you'll hear back within 2 hours.
                Reference: ITP-20241"
    """

    def __init__(self) -> None:
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

    async def run(self, request: ITRequest) -> ITProvisioningResult:
        """Route and execute an IT provisioning request."""
        import uuid
        request_id = f"ITP-{uuid.uuid4().hex[:5].upper()}"

        result = ITProvisioningResult(
            request_id=request_id,
            requester_upn=request.requester_upn,
            request_type=request.request_type,
            status="in_progress",
        )

        handlers = {
            RequestType.GROUP_ACCESS: self._handle_group_access,
            RequestType.LICENCE_CHANGE: self._handle_licence_change,
            RequestType.HARDWARE_REQUEST: self._handle_hardware_request,
            RequestType.VPN_CERTIFICATE: self._handle_vpn_cert,
            RequestType.TEMP_ELEVATED_ACCESS: self._handle_temp_access,
        }

        handler = handlers.get(request.request_type)
        if handler is None:
            result.status = "failed"
            result.errors.append(f"Unknown request type: {request.request_type}")
            return result

        try:
            await handler(request, result)
        except Exception as exc:
            logger.exception("IT provisioning failed for %s: %s", request_id, exc)
            result.status = "failed"
            result.errors.append(str(exc))

        logger.info(
            "IT request %s (%s) for %s → %s",
            request_id, request.request_type, request.requester_upn, result.status,
        )
        return result

    # ── Request handlers ────────────────────────────────────────────────────

    async def _handle_group_access(self, request: ITRequest, result: ITProvisioningResult) -> None:
        group_name: str = request.details["group_name"]
        requires_approval = group_name in HIGH_PRIVILEGE_GROUPS

        if requires_approval:
            ticket = await self._raise_approval_ticket(
                request, f"Add {request.requester_upn} to {group_name}"
            )
            result.pending_items.append(f"{group_name} (approval pending — {ticket})")
            result.status = "pending_approval"
            result.sla_minutes = 120
            await self.notify_skill.send_approval_request(
                requester=request.requester_upn,
                manager=request.details.get("manager_upn", ""),
                item=group_name,
                ticket_id=ticket,
                justification=request.justification or "No justification provided",
            )
        else:
            await self.azure_skill.add_user_to_group(
                upn=request.requester_upn,
                group_name=group_name,
            )
            result.fulfilled_items.append(f"Added to {group_name}")
            result.status = "fulfilled"
            result.sla_minutes = 5

    async def _handle_licence_change(self, request: ITRequest, result: ITProvisioningResult) -> None:
        licence: str = request.details["licence"]
        requires_approval = licence in APPROVAL_REQUIRED_LICENCES

        if requires_approval:
            ticket = await self._raise_approval_ticket(
                request, f"Assign {licence} to {request.requester_upn}"
            )
            result.pending_items.append(f"{licence} licence (approval pending — {ticket})")
            result.status = "pending_approval"
            result.sla_minutes = 120
        else:
            await self.azure_skill.assign_licence(
                upn=request.requester_upn,
                licence_sku=licence,
            )
            result.fulfilled_items.append(f"{licence} licence assigned")
            result.status = "fulfilled"
            result.sla_minutes = 15

    async def _handle_hardware_request(self, request: ITRequest, result: ITProvisioningResult) -> None:
        """Creates a pre-filled ServiceNow ticket for hardware fulfilment."""
        ticket_id = await self._create_servicenow_ticket(
            category="hardware",
            requester=request.requester_upn,
            details=request.details,
            justification=request.justification,
        )
        result.ticket_id = ticket_id
        result.pending_items.append(f"Hardware ticket {ticket_id} created (SLA: 2 business days)")
        result.status = "pending_approval"
        result.sla_minutes = 2880  # 2 business days

    async def _handle_vpn_cert(self, request: ITRequest, result: ITProvisioningResult) -> None:
        cert_serial = await self.azure_skill.renew_vpn_certificate(
            upn=request.requester_upn,
        )
        result.fulfilled_items.append(f"VPN certificate renewed (serial: {cert_serial})")
        result.status = "fulfilled"
        result.sla_minutes = 2
        await self.notify_skill.send_vpn_cert_notification(
            upn=request.requester_upn, cert_serial=cert_serial
        )

    async def _handle_temp_access(self, request: ITRequest, result: ITProvisioningResult) -> None:
        hours = min(
            int(request.details.get("duration_hours", TEMP_ACCESS_DEFAULT_HOURS)),
            TEMP_ACCESS_MAX_HOURS,
        )
        group_name: str = request.details["group_name"]
        expires_at = datetime.now(timezone.utc) + timedelta(hours=hours)

        ticket = await self._raise_approval_ticket(
            request, f"Temp access to {group_name} for {hours}h"
        )
        result.pending_items.append(f"Temp access to {group_name} (approval — {ticket})")
        result.expires_at = expires_at.isoformat()
        result.status = "pending_approval"
        result.sla_minutes = 30
        await self.notify_skill.send_approval_request(
            requester=request.requester_upn,
            manager=request.details.get("manager_upn", ""),
            item=f"Temporary {group_name} ({hours}h, auto-expires {expires_at:%Y-%m-%d %H:%M} UTC)",
            ticket_id=ticket,
            justification=request.justification or "No justification provided",
        )

    # ── Helpers ─────────────────────────────────────────────────────────────

    async def _raise_approval_ticket(self, request: ITRequest, summary: str) -> str:
        """Stub — in production this creates a real ServiceNow / Jira ticket."""
        import uuid
        return f"APPR-{uuid.uuid4().hex[:5].upper()}"

    async def _create_servicenow_ticket(
        self,
        category: str,
        requester: str,
        details: dict,
        justification: Optional[str],
    ) -> str:
        """Stub — in production calls ServiceNow REST API via Azure APIM."""
        import uuid
        return f"SN-{uuid.uuid4().hex[:6].upper()}"
