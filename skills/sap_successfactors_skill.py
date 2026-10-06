"""
Norvik Industries AB — SAP SuccessFactors Skill
IBM Client Engineering PoC · watsonx Orchestrate

Wraps Norvik's SAP SuccessFactors Employee Central API (via Azure APIM)
as a watsonx Orchestrate skill.

All API calls route through: https://apim.nordvik.se/sap/successfactors/v1/
Authentication: OAuth 2.0 Client Credentials (via Azure APIM policy)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

SF_API_VERSION = "v1"
REQUEST_TIMEOUT_SECONDS = 30


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class EmployeeRecord:
    employee_id: str
    full_name: str
    first_name: str
    last_name: str
    email: str
    department: str
    job_title: str
    cost_centre: str
    manager_id: str
    manager_email: str
    hire_date: str
    employment_status: str
    location: str


# ---------------------------------------------------------------------------
# Skill
# ---------------------------------------------------------------------------

class SuccessFactorsSkill:
    """
    watsonx Orchestrate skill — SAP SuccessFactors Employee Central.

    Registered in Orchestrate via openapi/successfactors_skill.yaml.
    """

    def __init__(self, base_url: str, client_id: str, client_secret: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self._token: Optional[str] = None
        self._http = httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS)

    # ── Authentication ───────────────────────────────────────────────────────

    async def _ensure_token(self) -> str:
        """Obtain or refresh the OAuth 2.0 access token via Azure APIM."""
        if self._token:
            return self._token
        resp = await self._http.post(
            f"{self.base_url}/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "scope": "successfactors.employee.read successfactors.sap.write",
            },
        )
        resp.raise_for_status()
        self._token = resp.json()["access_token"]
        return self._token

    async def _get(self, path: str, params: Optional[dict] = None) -> dict:
        token = await self._ensure_token()
        resp = await self._http.get(
            f"{self.base_url}/{path.lstrip('/')}",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
        )
        resp.raise_for_status()
        return resp.json()

    async def _post(self, path: str, json_body: dict) -> dict:
        token = await self._ensure_token()
        resp = await self._http.post(
            f"{self.base_url}/{path.lstrip('/')}",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=json_body,
        )
        resp.raise_for_status()
        return resp.json()

    async def _patch(self, path: str, json_body: dict) -> dict:
        token = await self._ensure_token()
        resp = await self._http.patch(
            f"{self.base_url}/{path.lstrip('/')}",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=json_body,
        )
        resp.raise_for_status()
        return resp.json()

    # ── Skill actions ────────────────────────────────────────────────────────

    async def get_employee(self, employee_id: str) -> EmployeeRecord:
        """
        Fetch an employee record from SuccessFactors by ID.

        Raises:
            httpx.HTTPStatusError: if the employee is not found (404) or API error.
        """
        logger.debug("Fetching SF employee %s", employee_id)
        data = await self._get(
            f"employees/{employee_id}",
            params={"$expand": "employmentNav,jobInfoNav,managerNav"},
        )
        emp = data.get("d", data)  # OData v2 envelope
        return EmployeeRecord(
            employee_id=emp["userId"],
            full_name=emp["displayName"],
            first_name=emp["firstName"],
            last_name=emp["lastName"],
            email=emp.get("email", f"{emp['firstName'].lower()}.{emp['lastName'].lower()}@nordvik.se"),
            department=emp.get("department", ""),
            job_title=emp.get("jobTitle", ""),
            cost_centre=emp.get("costCenter", ""),
            manager_id=emp.get("managerId", ""),
            manager_email=emp.get("managerEmail", ""),
            hire_date=emp.get("hireDate", ""),
            employment_status=emp.get("employmentStatus", "active"),
            location=emp.get("location", "Stockholm"),
        )

    async def request_sap_access(
        self,
        employee_id: str,
        sap_role: str,
        cost_centre: str,
        requires_approval: bool = True,
    ) -> str:
        """
        Submit an SAP S/4HANA access request for the employee.

        Returns the internal ticket/request ID.
        """
        logger.debug(
            "Requesting SAP role %s for employee %s (cost centre %s)",
            sap_role, employee_id, cost_centre,
        )
        body = {
            "employeeId": employee_id,
            "sapRole": sap_role,
            "costCentre": cost_centre,
            "requiresApproval": requires_approval,
            "requestedBy": "watsonx-orchestrate-agent",
        }
        result = await self._post("sap-access-requests", body)
        return result.get("requestId", "UNKNOWN")

    async def update_onboarding_status(
        self, employee_id: str, status: str, completed_at: str
    ) -> None:
        """Write back the automated onboarding completion status to SF."""
        logger.debug("Updating SF onboarding status for %s → %s", employee_id, status)
        await self._patch(
            f"employees/{employee_id}/onboarding",
            {
                "automatedOnboardingStatus": status,
                "automatedOnboardingCompletedAt": completed_at,
                "automatedOnboardingAgent": "ibm-wx-orchestrate-poc-v1",
            },
        )
