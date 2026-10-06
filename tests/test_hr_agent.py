"""
Tests — HR Onboarding Agent
Norvik Industries AB PoC · IBM Client Engineering
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agents.hr_onboarding_agent import HROnboardingAgent, OnboardingRequest, OnboardingResult
from skills.sap_successfactors_skill import EmployeeRecord


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_REQUEST = OnboardingRequest(
    employee_id="EMP-10842",
    first_name="Anna",
    last_name="Lindqvist",
    role="Senior Engineer",
    department="R&D",
    manager_email="erik.lindstrom@nordvik.se",
    start_date="2027-01-13",
    cost_centre="CC-RD-042",
)

SAMPLE_EMPLOYEE = EmployeeRecord(
    employee_id="EMP-10842",
    full_name="Anna Lindqvist",
    first_name="Anna",
    last_name="Lindqvist",
    email="a.lindqvist@nordvik.se",
    department="R&D",
    job_title="Senior Engineer",
    cost_centre="CC-RD-042",
    manager_id="EMP-10001",
    manager_email="erik.lindstrom@nordvik.se",
    hire_date="2027-01-13",
    employment_status="active",
    location="Stockholm",
)


@pytest.fixture
def mock_env(monkeypatch):
    """Patch environment variables required by the agent."""
    env_vars = {
        "SF_API_URL": "https://apim-test.nordvik.se/sap/successfactors/v1",
        "SF_CLIENT_ID": "test-sf-client-id",
        "SF_CLIENT_SECRET": "test-sf-client-secret",
        "AZURE_TENANT_ID": "test-tenant-id",
        "AZURE_CLIENT_ID": "test-azure-client-id",
        "AZURE_CLIENT_SECRET": "test-azure-client-secret",
        "SMTP_USER": "orchestrate@nordvik.se",
        "SMTP_PASSWORD": "test-smtp-password",
        "TEAMS_WEBHOOK_URL": "https://nordvik.webhook.office.com/webhookb2/test",
    }
    for key, value in env_vars.items():
        monkeypatch.setenv(key, value)


@pytest.fixture
def agent(mock_env):
    agent = HROnboardingAgent.__new__(HROnboardingAgent)
    agent.sf_skill = AsyncMock()
    agent.azure_skill = AsyncMock()
    agent.notify_skill = AsyncMock()
    return agent


# ---------------------------------------------------------------------------
# Happy-path tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_successful_onboarding_returns_completed(agent):
    """Full onboarding with no errors should return status='completed'."""
    agent.sf_skill.get_employee.return_value = SAMPLE_EMPLOYEE
    agent.sf_skill.request_sap_access.return_value = "SAP-REQ-20241-0042"
    agent.sf_skill.update_onboarding_status.return_value = None
    agent.azure_skill.create_user.return_value = MagicMock(upn="a.lindqvist@nordvik.se")
    agent.notify_skill.send_onboarding_welcome.return_value = None

    result: OnboardingResult = await agent.run(SAMPLE_REQUEST)

    assert result.status == "completed"
    assert result.azure_account_created is True
    assert result.sap_access_provisioned is True
    assert result.notification_sent is True
    assert result.errors == []


@pytest.mark.asyncio
async def test_onboarding_steps_are_recorded(agent):
    """All orchestration steps should be recorded in result.steps."""
    agent.sf_skill.get_employee.return_value = SAMPLE_EMPLOYEE
    agent.sf_skill.request_sap_access.return_value = "SAP-REQ-TEST"
    agent.sf_skill.update_onboarding_status.return_value = None
    agent.azure_skill.create_user.return_value = MagicMock()
    agent.notify_skill.send_onboarding_welcome.return_value = None

    result = await agent.run(SAMPLE_REQUEST)

    step_names = [s["step"] for s in result.steps]
    assert "fetch_sf_record" in step_names
    assert "create_azure_account" in step_names
    assert "provision_sap_access" in step_names
    assert "send_welcome" in step_names
    assert "update_sf_status" in step_names


@pytest.mark.asyncio
async def test_sap_access_is_pending_for_senior_engineer(agent):
    """Senior Engineer role requires approval → sap_approval_pending should be True."""
    agent.sf_skill.get_employee.return_value = SAMPLE_EMPLOYEE
    agent.sf_skill.request_sap_access.return_value = "SAP-REQ-PENDING"
    agent.sf_skill.update_onboarding_status.return_value = None
    agent.azure_skill.create_user.return_value = MagicMock()
    agent.notify_skill.send_onboarding_welcome.return_value = None

    result = await agent.run(SAMPLE_REQUEST)

    assert result.sap_approval_pending is True


# ---------------------------------------------------------------------------
# Failure-mode tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sf_fetch_failure_aborts_onboarding(agent):
    """If SuccessFactors is unreachable, onboarding should abort immediately."""
    agent.sf_skill.get_employee.side_effect = ConnectionError("SF API unreachable")

    result = await agent.run(SAMPLE_REQUEST)

    assert result.status == "failed"
    assert any("SuccessFactors fetch failed" in e for e in result.errors)
    agent.azure_skill.create_user.assert_not_called()


@pytest.mark.asyncio
async def test_azure_failure_returns_partial(agent):
    """Azure AD failure should not abort SAP + notification steps → partial result."""
    agent.sf_skill.get_employee.return_value = SAMPLE_EMPLOYEE
    agent.azure_skill.create_user.side_effect = Exception("Azure API rate limit")
    agent.sf_skill.request_sap_access.return_value = "SAP-REQ-OK"
    agent.sf_skill.update_onboarding_status.return_value = None
    agent.notify_skill.send_onboarding_welcome.return_value = None

    result = await agent.run(SAMPLE_REQUEST)

    assert result.status == "partial"
    assert result.azure_account_created is False
    assert result.sap_access_provisioned is True   # SAP still proceeds
    assert len(result.errors) == 1


@pytest.mark.asyncio
async def test_notification_failure_is_non_fatal(agent):
    """Notification failures should not affect overall status — warnings only."""
    agent.sf_skill.get_employee.return_value = SAMPLE_EMPLOYEE
    agent.sf_skill.request_sap_access.return_value = "SAP-REQ-OK"
    agent.sf_skill.update_onboarding_status.return_value = None
    agent.azure_skill.create_user.return_value = MagicMock()
    agent.notify_skill.send_onboarding_welcome.side_effect = Exception("SMTP timeout")

    result = await agent.run(SAMPLE_REQUEST)

    # Notification error goes into errors list, but status should still be partial/completed
    assert result.status in ("completed", "partial")
    assert result.notification_sent is False


# ---------------------------------------------------------------------------
# Role profile tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("role,expected_licence", [
    ("Senior Engineer", "E3+Copilot"),
    ("Engineer", "E3"),
    ("HR Coordinator", "E3"),
    ("Finance Analyst", "E3+PowerBI"),
    ("Unknown Role", "E3"),   # falls back to default
])
async def test_role_profile_licence_assignment(agent, role, expected_licence):
    """Each role should trigger the correct M365 licence via the access profile."""
    agent.sf_skill.get_employee.return_value = SAMPLE_EMPLOYEE
    agent.sf_skill.request_sap_access.return_value = "SAP-REQ-OK"
    agent.sf_skill.update_onboarding_status.return_value = None
    agent.azure_skill.create_user.return_value = MagicMock()
    agent.notify_skill.send_onboarding_welcome.return_value = None

    req = OnboardingRequest(
        employee_id="EMP-99999",
        first_name="Test",
        last_name="User",
        role=role,
        department="Test",
        manager_email="manager@nordvik.se",
        start_date="2027-02-01",
        cost_centre="CC-TEST-001",
    )
    await agent.run(req)

    call_args = agent.azure_skill.create_user.call_args
    assert call_args is not None
    provisioning_request = call_args[0][0]
    assert provisioning_request.licence == expected_licence
