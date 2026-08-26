"""Verimailx email verification, exposed to AI agents over the Model Context Protocol.

The Actor runs a FastMCP server on the platform's web server port. With Standby
enabled, Apify keeps an instance warm and routes MCP requests to it, so any MCP
client (Claude, Cursor, an agent framework) can call the tools below at
    https://cold-email-master--verimailx-email-mcp.apify.actor/mcp
"""

import asyncio
import os

import httpx
import uvicorn
from fastmcp import FastMCP

from apify import Actor

# Verimailx REST API. The key is supplied by the Actor's environment, so callers
# authenticate with their Apify token and never need a Verimailx account.
API_KEY_ENV = 'APIFY-VERIMAILX-API-KEY'
VERIMAILX_BASE = 'https://api.verimailx.com'
VALIDATE_ENDPOINT = f'{VERIMAILX_BASE}/validate'
BULK_ENDPOINT = f'{VERIMAILX_BASE}/bulk-validate'

# Most agents verify a handful of addresses at a time. Keeping the ceiling low
# keeps tool calls fast and their cost predictable for the caller.
LIST_MAX = 100

# Pay-per-event charge, fired once per address that reaches a real verdict.
CHARGE_EVENT = 'email-verified'

REQUEST_TIMEOUT = httpx.Timeout(60.0, connect=10.0)


def _api_key() -> str:
    key = os.environ.get(API_KEY_ENV)
    if not key:
        raise RuntimeError(
            f'{API_KEY_ENV} is not configured on this Actor, so verification is unavailable. '
            'Set it in the Apify console under Environment variables.'
        )
    return key


def _shape(item: dict) -> dict:
    """Flatten a Verimailx record into the fields an agent actually reasons about."""
    return {
        'email': item.get('email'),
        'verdict': item.get('result'),
        'deliverability_score': item.get('deliverability_score'),
        'syntax_valid': item.get('is_syntax_valid'),
        'dns_valid': item.get('is_dns_valid'),
        'mx_valid': item.get('is_mx_valid'),
        'smtp_valid': item.get('is_smtp_valid'),
        'disposable': item.get('is_disposable'),
        'role_based': item.get('is_role_based'),
        'mx_hosts': item.get('mx_hosts', []),
    }


async def _post(client: httpx.AsyncClient, url: str, payload: dict) -> dict:
    response = await client.post(
        url,
        json=payload,
        headers={'X-API-Key': _api_key(), 'Content-Type': 'application/json'},
    )
    if response.status_code >= 400:
        raise RuntimeError(f'Verimailx returned {response.status_code}: {response.text[:200]}')
    return response.json()


def build_server() -> FastMCP:
    server = FastMCP(name='verimailx-email-verifier')

    @server.tool()
    async def verify_email(email: str) -> dict:
        """Verify that a single email address can receive mail.

        Runs RFC syntax, DNS, MX and SMTP-handshake checks and detects disposable
        and role-based mailboxes. No message is ever sent to the address.

        Returns a verdict of valid, invalid, risky or unknown, a deliverability
        score from 0 to 100, and the individual check results. Use this before
        adding an address to an outreach list or a CRM record.
        """
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            payload = await _post(client, VALIDATE_ENDPOINT, {'email': email})

        item = payload.get('result') if isinstance(payload.get('result'), dict) else payload
        await Actor.charge(event_name=CHARGE_EVENT)
        return _shape(item)

    @server.tool()
    async def verify_email_list(emails: list[str]) -> dict:
        """Verify a list of email addresses in one call.

        Same checks as verify_email, run as a batch. Duplicates are removed before
        verification. Accepts up to 100 addresses per call — for larger lists, use
        the bulk Actor at apify.com/cold_email_master/email-verifier-validator,
        which accepts a CSV link and handles tens of thousands of addresses.

        Returns one record per address plus a summary count of each verdict, so you
        can report how many addresses are safe to send to.
        """
        deduped = list(dict.fromkeys(e.strip().lower() for e in emails if e and e.strip()))

        if not deduped:
            raise ValueError('No email addresses were provided.')
        if len(deduped) > LIST_MAX:
            raise ValueError(
                f'{len(deduped)} addresses were provided but this tool accepts at most {LIST_MAX}. '
                'For larger lists use the bulk Actor: apify.com/cold_email_master/email-verifier-validator'
            )

        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            payload = await _post(client, BULK_ENDPOINT, {'emails': deduped})

        results = payload.get('results')
        if not isinstance(results, list):
            raise RuntimeError('Verimailx returned an unexpected response shape.')

        shaped = [_shape(item) for item in results]

        # One charge call per address rather than a single batched call. Apify
        # recommends this: it keeps the caller's spending limit enforced per
        # unit, so a run that hits its cap stops cleanly instead of overshooting.
        for _ in shaped:
            await Actor.charge(event_name=CHARGE_EVENT)

        summary: dict[str, int] = {}
        for record in shaped:
            verdict = record.get('verdict') or 'unknown'
            summary[verdict] = summary.get(verdict, 0) + 1

        return {'checked': len(shaped), 'summary': summary, 'results': shaped}

    @server.resource(uri='resource://verimailx/info', name='verimailx-info')
    def info() -> str:
        """Describe what this MCP server does."""
        return (
            'Verimailx email verification. verify_email checks one address; '
            'verify_email_list checks up to 100 at once. Each check runs RFC syntax, '
            'DNS, MX and SMTP-handshake validation and flags disposable and '
            'role-based mailboxes, without sending any mail. Billed per address verified.'
        )

    return server


async def main() -> None:
    async with Actor:
        server = build_server()
        app = server.http_app(transport='streamable-http')

        config = uvicorn.Config(
            app,
            host='0.0.0.0',  # noqa: S104 — required so the platform can route to the container
            port=Actor.configuration.web_server_port,
        )
        web_server = uvicorn.Server(config)
        server_task = asyncio.create_task(web_server.serve())

        Actor.log.info(f'MCP server ready at {Actor.configuration.web_server_url}/mcp')

        # Serve until the platform shuts the Actor down — with Standby that happens
        # automatically once the instance has been idle past its timeout.
        await server_task


if __name__ == '__main__':
    asyncio.run(main())
