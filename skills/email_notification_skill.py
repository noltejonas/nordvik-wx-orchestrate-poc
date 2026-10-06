"""
Norvik Industries AB — Email & Teams Notification Skill
IBM Client Engineering PoC · watsonx Orchestrate

Sends onboarding welcome emails, Teams adaptive cards,
VPN certificate notifications, and approval request messages.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from dataclasses import dataclass
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class OnboardingNotification:
    recipient_name: str
    recipient_email: str
    manager_email: str
    start_date: str
    role: str
    department: str
    azure_account_created: bool = True
    sap_approval_pending: bool = True


# ---------------------------------------------------------------------------
# Skill
# ---------------------------------------------------------------------------

class NotificationSkill:
    """
    watsonx Orchestrate skill — Email & Microsoft Teams notifications.

    Sends:
      - Welcome emails (SMTP / Exchange Online)
      - Teams adaptive cards (incoming webhook)
      - VPN cert renewal confirmations
      - Approval request messages
    """

    def __init__(
        self,
        smtp_host: str,
        smtp_port: int,
        smtp_user: str,
        smtp_password: str,
        teams_webhook_url: Optional[str] = None,
        sender_name: str = "IBM watsonx Orchestrate",
    ) -> None:
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.smtp_user = smtp_user
        self.smtp_password = smtp_password
        self.teams_webhook_url = teams_webhook_url
        self.sender_name = sender_name
        self._http = httpx.AsyncClient(timeout=15)

    # ── Public actions ───────────────────────────────────────────────────────

    async def send_onboarding_welcome(self, notification: OnboardingNotification) -> None:
        """Send welcome email + Teams adaptive card for a new hire."""
        await self._send_email(
            to=[notification.recipient_email, notification.manager_email],
            subject=f"🎉 Welcome to Norvik Industries, {notification.recipient_name.split()[0]}!",
            html_body=self._build_welcome_email(notification),
        )
        if self.teams_webhook_url:
            await self._send_teams_card(self._build_welcome_card(notification))
        logger.info("Onboarding welcome sent to %s", notification.recipient_email)

    async def send_approval_request(
        self,
        requester: str,
        manager: str,
        item: str,
        ticket_id: str,
        justification: str,
    ) -> None:
        """Notify a manager that their approval is needed."""
        await self._send_email(
            to=[manager],
            subject=f"[Action Required] Access Approval: {item} ({ticket_id})",
            html_body=self._build_approval_email(requester, item, ticket_id, justification),
        )
        if self.teams_webhook_url:
            await self._send_teams_card(
                self._build_approval_card(requester, item, ticket_id, justification)
            )
        logger.info("Approval request %s sent to %s", ticket_id, manager)

    async def send_vpn_cert_notification(self, upn: str, cert_serial: str) -> None:
        """Notify user that their VPN certificate has been renewed."""
        await self._send_email(
            to=[upn],
            subject=f"✅ Your VPN certificate has been renewed ({cert_serial})",
            html_body=self._build_vpn_email(upn, cert_serial),
        )
        logger.info("VPN cert renewal notification sent to %s", upn)

    # ── Email builder ────────────────────────────────────────────────────────

    def _build_welcome_email(self, n: OnboardingNotification) -> str:
        sap_note = (
            "⏳ Your SAP access is pending manager approval and will be activated within 2 hours."
            if n.sap_approval_pending
            else "✅ Your SAP S/4HANA access has been provisioned."
        )
        return f"""
        <html><body style="font-family:Segoe UI,sans-serif;color:#1f2328;max-width:600px">
        <h2 style="color:#0f62fe">Welcome to Norvik Industries, {n.recipient_name.split()[0]}! 🎉</h2>
        <p>We're excited to have you join us as <strong>{n.role}</strong>
           in the <strong>{n.department}</strong> team, starting <strong>{n.start_date}</strong>.</p>

        <h3>Your accounts are ready:</h3>
        <ul>
          {'<li>✅ Microsoft 365 account: <strong>' + n.recipient_email + '</strong></li>' if n.azure_account_created else ''}
          <li>{sap_note}</li>
        </ul>

        <h3>Next steps:</h3>
        <ol>
          <li>Sign in at <a href="https://portal.office.com">portal.office.com</a> with your new email</li>
          <li>You will be prompted to change your temporary password on first login</li>
          <li>Join the <strong>#new-joiners</strong> Teams channel to connect with your colleagues</li>
          <li>Your manager ({n.manager_email}) will reach out to schedule your first-week plan</li>
        </ol>

        <p style="color:#57606a;font-size:12px">
          This message was sent automatically by IBM watsonx Orchestrate on behalf of Norvik IT.
          Questions? Contact <a href="mailto:it-support@nordvik.se">it-support@nordvik.se</a>
        </p>
        </body></html>
        """

    def _build_approval_email(
        self, requester: str, item: str, ticket_id: str, justification: str
    ) -> str:
        return f"""
        <html><body style="font-family:Segoe UI,sans-serif;color:#1f2328;max-width:600px">
        <h2 style="color:#da1e28">⚠️ Approval Required: {item}</h2>
        <p><strong>{requester}</strong> has requested access to a privileged resource
           and requires your approval.</p>
        <table style="border-collapse:collapse;width:100%">
          <tr><td style="padding:8px;border:1px solid #e5e7eb"><strong>Ticket</strong></td>
              <td style="padding:8px;border:1px solid #e5e7eb">{ticket_id}</td></tr>
          <tr><td style="padding:8px;border:1px solid #e5e7eb"><strong>Resource</strong></td>
              <td style="padding:8px;border:1px solid #e5e7eb">{item}</td></tr>
          <tr><td style="padding:8px;border:1px solid #e5e7eb"><strong>Justification</strong></td>
              <td style="padding:8px;border:1px solid #e5e7eb">{justification}</td></tr>
        </table>
        <p>Please log in to the <a href="https://portal.nordvik.se/approvals">Norvik IT Portal</a>
           to approve or reject this request. Auto-approvals are disabled for this resource type.</p>
        <p style="color:#57606a;font-size:12px">Sent by IBM watsonx Orchestrate IT Provisioning Agent</p>
        </body></html>
        """

    def _build_vpn_email(self, upn: str, cert_serial: str) -> str:
        return f"""
        <html><body style="font-family:Segoe UI,sans-serif;color:#1f2328;max-width:600px">
        <h2 style="color:#24a148">✅ VPN Certificate Renewed</h2>
        <p>Your Norvik VPN certificate has been automatically renewed.</p>
        <p><strong>New serial:</strong> <code>{cert_serial}</code></p>
        <p>Your VPN client will pick up the new certificate automatically on next connection.
           If you experience issues, please contact <a href="mailto:it-support@nordvik.se">IT Support</a>.</p>
        </body></html>
        """

    # ── Teams adaptive card builders ─────────────────────────────────────────

    def _build_welcome_card(self, n: OnboardingNotification) -> dict:
        return {
            "type": "message",
            "attachments": [{
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": [
                        {"type": "TextBlock", "size": "Large", "weight": "Bolder",
                         "text": f"🎉 Welcome, {n.recipient_name.split()[0]}!"},
                        {"type": "TextBlock", "wrap": True,
                         "text": f"Starting **{n.start_date}** as **{n.role}** in **{n.department}**"},
                        {"type": "FactSet", "facts": [
                            {"title": "Email", "value": n.recipient_email},
                            {"title": "SAP Access", "value": "⏳ Pending approval" if n.sap_approval_pending else "✅ Active"},
                            {"title": "M365", "value": "✅ Active"},
                        ]},
                    ],
                    "actions": [{
                        "type": "Action.OpenUrl",
                        "title": "Open IT Portal",
                        "url": "https://portal.nordvik.se",
                    }],
                },
            }],
        }

    def _build_approval_card(
        self, requester: str, item: str, ticket_id: str, justification: str
    ) -> dict:
        return {
            "type": "message",
            "attachments": [{
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard", "version": "1.4",
                    "body": [
                        {"type": "TextBlock", "size": "Large", "weight": "Bolder",
                         "color": "Warning", "text": f"⚠️ Approval Required ({ticket_id})"},
                        {"type": "FactSet", "facts": [
                            {"title": "Requested by", "value": requester},
                            {"title": "Resource", "value": item},
                            {"title": "Justification", "value": justification},
                        ]},
                    ],
                    "actions": [
                        {"type": "Action.OpenUrl", "title": "Approve / Reject",
                         "url": f"https://portal.nordvik.se/approvals/{ticket_id}"},
                    ],
                },
            }],
        }

    # ── Transport ────────────────────────────────────────────────────────────

    async def _send_email(self, to: list[str], subject: str, html_body: str) -> None:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"{self.sender_name} <{self.smtp_user}>"
        msg["To"] = ", ".join(to)
        msg.attach(MIMEText(html_body, "html"))

        context = ssl.create_default_context()
        with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
            server.ehlo()
            server.starttls(context=context)
            server.login(self.smtp_user, self.smtp_password)
            server.sendmail(self.smtp_user, to, msg.as_string())

    async def _send_teams_card(self, card: dict) -> None:
        if not self.teams_webhook_url:
            return
        resp = await self._http.post(self.teams_webhook_url, json=card)
        resp.raise_for_status()
