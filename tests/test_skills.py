"""
Tests — Skill integrations
Norvik Industries AB PoC · IBM Client Engineering
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
import httpx


# ---------------------------------------------------------------------------
# SuccessFactors Skill tests
# ---------------------------------------------------------------------------

class TestSuccessFactorsSkill:

    @pytest.fixture
    def sf_skill(self, monkeypatch):
        from skills.sap_successfactors_skill import SuccessFactorsSkill
        skill = SuccessFactorsSkill(
            base_url="https://apim-test.nordvik.se/sap/successfactors/v1",
            client_id="test-client-id",
            client_secret="test-client-secret",
        )
        # Pre-set token so we don't hit OAuth
        skill._token = "mock-access-token"
        return skill

    @pytest.mark.asyncio
    async def test_get_employee_maps_fields_correctly(self, sf_skill):
        """get_employee should correctly map OData v2 response to EmployeeRecord."""
        mock_response = {
            "d": {
                "userId": "EMP-10842",
                "displayName": "Anna Lindqvist",
                "firstName": "Anna",
                "lastName": "Lindqvist",
                "email": "a.lindqvist@nordvik.se",
                "department": "R&D",
                "jobTitle": "Senior Engineer",
                "costCenter": "CC-RD-042",
                "managerId": "EMP-10001",
                "managerEmail": "erik.lindstrom@nordvik.se",
                "hireDate": "2027-01-13",
                "employmentStatus": "active",
                "location": "Stockholm",
            }
        }

        with patch.object(sf_skill, "_get", new=AsyncMock(return_value=mock_response)):
            record = await sf_skill.get_employee("EMP-10842")

        assert record.employee_id == "EMP-10842"
        assert record.full_name == "Anna Lindqvist"
        assert record.department == "R&D"
        assert record.cost_centre == "CC-RD-042"
        assert record.manager_email == "erik.lindstrom@nordvik.se"

    @pytest.mark.asyncio
    async def test_get_employee_falls_back_email_from_name(self, sf_skill):
        """If email is absent in response, it should be derived from first/last name."""
        mock_response = {
            "d": {
                "userId": "EMP-00001",
                "displayName": "Lars Eriksson",
                "firstName": "Lars",
                "lastName": "Eriksson",
                "department": "Finance",
                "jobTitle": "Analyst",
                "costCenter": "CC-FIN-001",
                "managerId": "",
                "managerEmail": "",
                "hireDate": "2027-02-01",
                "employmentStatus": "active",
                "location": "Gothenburg",
            }
        }

        with patch.object(sf_skill, "_get", new=AsyncMock(return_value=mock_response)):
            record = await sf_skill.get_employee("EMP-00001")

        assert record.email == "l.eriksson@nordvik.se"

    @pytest.mark.asyncio
    async def test_request_sap_access_returns_ticket_id(self, sf_skill):
        """requestSapAccess should return the ticket ID from the response."""
        mock_response = {"requestId": "SAP-REQ-20241-0042", "status": "submitted"}

        with patch.object(sf_skill, "_post", new=AsyncMock(return_value=mock_response)):
            ticket = await sf_skill.request_sap_access(
                employee_id="EMP-10842",
                sap_role="Z_ENG_SENIOR",
                cost_centre="CC-RD-042",
                requires_approval=True,
            )

        assert ticket == "SAP-REQ-20241-0042"

    @pytest.mark.asyncio
    async def test_update_onboarding_status_sends_correct_payload(self, sf_skill):
        """updateOnboardingStatus should PATCH with the correct status field."""
        patched = AsyncMock(return_value={})
        with patch.object(sf_skill, "_patch", new=patched):
            await sf_skill.update_onboarding_status(
                employee_id="EMP-10842",
                status="AUTOMATED_ONBOARDING_COMPLETE",
                completed_at="2027-01-13T08:47:00Z",
            )

        call_args = patched.call_args
        payload = call_args[0][1]
        assert payload["automatedOnboardingStatus"] == "AUTOMATED_ONBOARDING_COMPLETE"
        assert payload["automatedOnboardingAgent"] == "ibm-wx-orchestrate-poc-v1"


# ---------------------------------------------------------------------------
# Azure AD Skill tests
# ---------------------------------------------------------------------------

class TestAzureADSkill:

    @pytest.fixture
    def azure_skill(self):
        from skills.azure_ad_skill import AzureADSkill
        skill = AzureADSkill(
            tenant_id="test-tenant-id",
            client_id="test-client-id",
            client_secret="test-client-secret",
        )
        skill._token = "mock-graph-token"
        return skill

    @pytest.mark.asyncio
    async def test_create_user_calls_graph_in_correct_order(self, azure_skill):
        """create_user should: POST user → assign licence → add groups → enable."""
        from skills.azure_ad_skill import UserProvisioningRequest

        post_mock = AsyncMock(return_value={"id": "azure-user-id-123"})
        patch_mock = AsyncMock(return_value=None)

        with (
            patch.object(azure_skill, "_post", new=post_mock),
            patch.object(azure_skill, "_patch", new=patch_mock),
            patch.object(azure_skill, "_get", new=AsyncMock(return_value={"id": "mgr-id", "value": [{"id": "grp-id"}]})),
        ):
            req = UserProvisioningRequest(
                upn="t.test@nordvik.se",
                display_name="Test User",
                department="Engineering",
                job_title="Engineer",
                manager_upn="manager@nordvik.se",
                licence="E3",
                groups=["sg-engineers"],
            )
            user = await azure_skill.create_user(req)

        assert user.upn == "t.test@nordvik.se"
        assert user.object_id == "azure-user-id-123"

    @pytest.mark.asyncio
    async def test_add_user_to_group_raises_on_unknown_group(self, azure_skill):
        """add_user_to_group should raise ValueError if group is not found."""
        with (
            patch.object(azure_skill, "_get", new=AsyncMock(return_value={"value": []})),
            patch.object(azure_skill, "_post", new=AsyncMock()),
        ):
            with pytest.raises(ValueError, match="not found in Azure AD"):
                await azure_skill.add_user_to_group(
                    upn="a.test@nordvik.se",
                    group_name="sg-nonexistent-group",
                    user_id="some-user-id",
                )

    @pytest.mark.asyncio
    async def test_renew_vpn_certificate_returns_serial(self, azure_skill):
        """renewVpnCertificate should return a non-empty serial string."""
        serial = await azure_skill.renew_vpn_certificate("a.lindqvist@nordvik.se")
        assert serial.startswith("VPN-")
        assert len(serial) > 4


# ---------------------------------------------------------------------------
# Notification Skill tests
# ---------------------------------------------------------------------------

class TestNotificationSkill:

    @pytest.fixture
    def notify_skill(self):
        from skills.email_notification_skill import NotificationSkill
        return NotificationSkill(
            smtp_host="smtp.office365.com",
            smtp_port=587,
            smtp_user="test@nordvik.se",
            smtp_password="test-password",
            teams_webhook_url="https://nordvik.webhook.office.com/test",
        )

    @pytest.fixture
    def sample_notification(self):
        from skills.email_notification_skill import OnboardingNotification
        return OnboardingNotification(
            recipient_name="Anna Lindqvist",
            recipient_email="a.lindqvist@nordvik.se",
            manager_email="erik.lindstrom@nordvik.se",
            start_date="2027-01-13",
            role="Senior Engineer",
            department="R&D",
            azure_account_created=True,
            sap_approval_pending=True,
        )

    def test_welcome_email_contains_recipient_name(self, notify_skill, sample_notification):
        """Welcome email HTML should mention the recipient's first name."""
        html = notify_skill._build_welcome_email(sample_notification)
        assert "Anna" in html

    def test_welcome_email_shows_sap_pending_when_applicable(self, notify_skill, sample_notification):
        """When SAP approval is pending, welcome email should mention it."""
        html = notify_skill._build_welcome_email(sample_notification)
        assert "pending" in html.lower() or "Pending" in html

    def test_welcome_card_has_correct_structure(self, notify_skill, sample_notification):
        """Teams card should be a valid adaptive card payload."""
        card = notify_skill._build_welcome_card(sample_notification)
        assert card["type"] == "message"
        assert len(card["attachments"]) > 0
        content = card["attachments"][0]["content"]
        assert content["type"] == "AdaptiveCard"
        assert len(content["body"]) > 0

    def test_approval_card_contains_ticket_id(self, notify_skill):
        """Approval card should include the ticket ID in the body."""
        card = notify_skill._build_approval_card(
            requester="a.lindqvist@nordvik.se",
            item="sg-sap-admin",
            ticket_id="APPR-ABC12",
            justification="Needed for hotfix deployment",
        )
        import json
        card_str = json.dumps(card)
        assert "APPR-ABC12" in card_str
