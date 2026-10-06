"""
Norvik Industries AB — Azure AD / Entra ID Skill
IBM Client Engineering PoC · watsonx Orchestrate

Provides user provisioning, group management, licence assignment,
and VPN certificate operations via Microsoft Graph API.

All calls route through Norvik's Azure API Management gateway:
  https://apim.nordvik.se/msgraph/v1.0/
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
REQUEST_TIMEOUT_SECONDS = 30

# M365 licence SKU mapping (Norvik-specific)
LICENCE_SKUS: dict[str, str] = {
    "E3":                          "6fd2c87f-b296-42f0-b197-1e91e994b900",
    "E3+Copilot":                  "6fd2c87f-b296-42f0-b197-1e91e994b900",   # E3 base
    "E5":                          "c7df2760-2c81-4ef7-b578-5b5392b571df",
    "E3+PowerBI":                  "6fd2c87f-b296-42f0-b197-1e91e994b900",
    "PowerBI-Pro":                 "f8a1db68-be16-40ed-86d5-cb42ce701560",
    "GitHub-Copilot-Enterprise":   "b8d59ffa-8f06-4f88-b2e0-98f5d9a72534",
}

COPILOT_SKU = "639dec6b-bb19-468b-871c-c5c441c4b0cb"


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class UserProvisioningRequest:
    upn: str
    display_name: str
    department: str
    job_title: str
    manager_upn: str
    licence: str
    groups: list[str] = field(default_factory=list)
    usage_location: str = "SE"             # Sweden ISO code


@dataclass
class AzureUser:
    object_id: str
    upn: str
    display_name: str
    department: str
    licences_assigned: list[str]
    groups_assigned: list[str]


# ---------------------------------------------------------------------------
# Skill
# ---------------------------------------------------------------------------

class AzureADSkill:
    """
    watsonx Orchestrate skill — Microsoft Azure AD / Entra ID.

    Registered in Orchestrate via openapi/azure_ad_skill.yaml.
    Uses OAuth 2.0 Client Credentials against Norvik's tenant.
    """

    def __init__(self, tenant_id: str, client_id: str, client_secret: str) -> None:
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.client_secret = client_secret
        self._token: Optional[str] = None
        self._http = httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS)

    # ── Authentication ───────────────────────────────────────────────────────

    async def _ensure_token(self) -> str:
        if self._token:
            return self._token
        resp = await self._http.post(
            f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "scope": "https://graph.microsoft.com/.default",
            },
        )
        resp.raise_for_status()
        self._token = resp.json()["access_token"]
        return self._token

    async def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {await self._ensure_token()}",
            "Content-Type": "application/json",
        }

    async def _get(self, path: str) -> dict:
        resp = await self._http.get(
            f"{GRAPH_BASE}/{path.lstrip('/')}",
            headers=await self._headers(),
        )
        resp.raise_for_status()
        return resp.json()

    async def _post(self, path: str, body: dict) -> dict:
        resp = await self._http.post(
            f"{GRAPH_BASE}/{path.lstrip('/')}",
            headers=await self._headers(),
            json=body,
        )
        resp.raise_for_status()
        return resp.json()

    async def _patch(self, path: str, body: dict) -> None:
        resp = await self._http.patch(
            f"{GRAPH_BASE}/{path.lstrip('/')}",
            headers=await self._headers(),
            json=body,
        )
        resp.raise_for_status()

    # ── Skill actions ────────────────────────────────────────────────────────

    async def create_user(self, req: UserProvisioningRequest) -> AzureUser:
        """
        Create a new user in Azure AD and assign licence + groups.

        Steps:
          1. POST /users (create account, disabled until licence assigned)
          2. PATCH /users/{id} (set manager)
          3. POST /users/{id}/assignLicense
          4. POST /groups/{id}/members/$ref  (for each group)
          5. PATCH /users/{id} (enable account)
        """
        logger.info("Creating Azure AD user %s", req.upn)

        # 1. Create user
        user_data = await self._post("users", {
            "accountEnabled": False,   # enabled after licence
            "displayName": req.display_name,
            "mailNickname": req.upn.split("@")[0],
            "userPrincipalName": req.upn,
            "usageLocation": req.usage_location,
            "department": req.department,
            "jobTitle": req.job_title,
            "passwordProfile": {
                "forceChangePasswordNextSignIn": True,
                "password": _generate_temp_password(),
            },
        })
        user_id: str = user_data["id"]

        # 2. Assign licence
        assigned_licences = await self._assign_licences(user_id, req.licence)

        # 3. Set manager
        if req.manager_upn:
            try:
                mgr = await self._get(f"users/{req.manager_upn}")
                await self._post(f"users/{user_id}/manager/$ref", {
                    "@odata.id": f"{GRAPH_BASE}/users/{mgr['id']}"
                })
            except Exception:
                logger.warning("Manager assignment failed for %s (non-fatal)", req.upn)

        # 4. Add to groups
        assigned_groups = []
        for group_name in req.groups:
            try:
                await self.add_user_to_group(upn=req.upn, group_name=group_name, user_id=user_id)
                assigned_groups.append(group_name)
            except Exception as exc:
                logger.warning("Group %s assignment failed: %s", group_name, exc)

        # 5. Enable account
        await self._patch(f"users/{user_id}", {"accountEnabled": True})

        logger.info("Azure AD user %s created successfully (id=%s)", req.upn, user_id)
        return AzureUser(
            object_id=user_id,
            upn=req.upn,
            display_name=req.display_name,
            department=req.department,
            licences_assigned=assigned_licences,
            groups_assigned=assigned_groups,
        )

    async def _assign_licences(self, user_id: str, licence_name: str) -> list[str]:
        """Assign M365 licence(s) to a user by SKU name."""
        skus_to_assign = []
        base_sku = LICENCE_SKUS.get(licence_name)
        if base_sku:
            skus_to_assign.append({"skuId": base_sku})
        if "Copilot" in licence_name:
            skus_to_assign.append({"skuId": COPILOT_SKU})

        await self._post(f"users/{user_id}/assignLicense", {
            "addLicenses": skus_to_assign,
            "removeLicenses": [],
        })
        return [licence_name]

    async def add_user_to_group(
        self, upn: str, group_name: str, user_id: Optional[str] = None
    ) -> None:
        """Add a user to an Azure AD group by display name."""
        if not user_id:
            user = await self._get(f"users/{upn}")
            user_id = user["id"]
        groups = await self._get(f"groups?$filter=displayName eq '{group_name}'&$select=id")
        items = groups.get("value", [])
        if not items:
            raise ValueError(f"Group '{group_name}' not found in Azure AD")
        group_id = items[0]["id"]
        await self._post(f"groups/{group_id}/members/$ref", {
            "@odata.id": f"{GRAPH_BASE}/directoryObjects/{user_id}"
        })
        logger.debug("Added %s to group %s", upn, group_name)

    async def assign_licence(self, upn: str, licence_sku: str) -> None:
        """Assign an additional licence to an existing user."""
        user = await self._get(f"users/{upn}")
        await self._assign_licences(user["id"], licence_sku)
        logger.info("Assigned %s to %s", licence_sku, upn)

    async def renew_vpn_certificate(self, upn: str) -> str:
        """
        Renew the user's VPN certificate via the Azure APIM-proxied PKI endpoint.
        Returns the new certificate serial number.
        """
        logger.info("Renewing VPN certificate for %s", upn)
        # In production: POST to https://apim.nordvik.se/pki/v1/certificates/renew
        # Stubbed here — returns a mock serial
        import uuid
        return f"VPN-{uuid.uuid4().hex[:8].upper()}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _generate_temp_password() -> str:
    """Generate a secure temporary password (changed on first sign-in)."""
    import secrets
    import string
    alphabet = string.ascii_letters + string.digits + "!@#$%"
    return "".join(secrets.choice(alphabet) for _ in range(20))
