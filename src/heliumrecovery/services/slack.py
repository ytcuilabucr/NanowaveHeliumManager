"""Slack sender (chat.postMessage with a bot token).

What gets sent, and how often, is decided in services/alerts.py.

Secrets: ``SLACK_BOT_TOKEN`` and ``SLACK_CHANNEL``. Without them nothing is sent.
"""

from __future__ import annotations

import httpx

SLACK_URL = "https://slack.com/api/chat.postMessage"


class SlackNotifier:
    def __init__(self, token: str, channel: str, *, client: httpx.AsyncClient) -> None:
        self.token, self.channel, self.client = token, channel, client

    async def send(self, text: str) -> None:
        resp = await self.client.post(
            SLACK_URL,
            headers={"Authorization": f"Bearer {self.token}"},
            json={"channel": self.channel, "text": text, "as_user": True},
            timeout=10,
        )
        resp.raise_for_status()
        body = resp.json()
        if not body.get("ok"):
            raise httpx.HTTPError(f"slack error: {body.get('error')}")
