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
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from apify import Actor, Event

BULK_ACTOR_URL = 'apify.com/cold_email_master/bulk-email-verifier-validator'

# Verimailx REST API. The key is supplied by the Actor's environment, so callers
# authenticate with their Apify token and never need a Verimailx account.
API_KEY_ENV = 'APIFY-VERIMAILX-API-KEY'
VERIMAILX_BASE = 'https://api.verimailx.com'
VALIDATE_ENDPOINT = f'{VERIMAILX_BASE}/validate'

# The /bulk-validate endpoint is asynchronous: it returns a job id and the
# caller polls until a CSV is ready, which can take minutes. An MCP tool call
# has to answer while the agent is still waiting on it, so this server verifies
# a list by calling the synchronous single-address endpoint concurrently and
# points callers at the bulk Actor once the list gets large.
LIST_MAX = 25
LIST_CONCURRENCY = 10

# Pay-per-event charge, fired once per address that reaches a real verdict.
CHARGE_EVENT = 'email-verified'

# Standby gives an incoming request 5 minutes before it times out. A full list
# runs in ceil(LIST_MAX / LIST_CONCURRENCY) waves, so the per-request timeout has
# to leave the whole batch comfortably inside that ceiling: 3 waves x 30s = 90s.
# A real address answers in about 5 seconds, so 30s is already generous.
REQUEST_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

# A catch-all domain accepts mail for every address, so a positive result there
# proves nothing. Disposable and role-based mailboxes are deliverable but are a
# bad idea to send to. All three are reported as risky rather than valid.
_RISKY = {'risky', 'catch_all', 'catch-all', 'catchall', 'disposable', 'role', 'role_based'}


def _overall(verdict: object) -> str:
    """Collapse the API's verdict into valid / invalid / risky / unknown."""
    v = str(verdict or '').strip().lower()
    if v == 'valid':
        return 'valid'
    if v == 'invalid':
        return 'invalid'
    if v in _RISKY:
        return 'risky'
    return 'unknown'


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
    verdict = item.get('result')
    overall = _overall(verdict)
    return {
        'email': item.get('email'),
        # The question an agent is actually asking. True only for a clean valid.
        'safe_to_send': overall == 'valid',
        'overall': overall,
        'verdict': verdict,
        'deliverability_score': item.get('deliverability_score'),
        'syntax_valid': item.get('is_syntax_valid'),
        'dns_valid': item.get('is_dns_valid'),
        'mx_valid': item.get('is_mx_valid'),
        'smtp_valid': item.get('is_smtp_valid'),
        'catch_all': overall == 'risky' and str(verdict or '').lower().replace('-', '_') == 'catch_all',
        'disposable': item.get('is_disposable'),
        'role_based': item.get('is_role_based'),
        'mx_hosts': item.get('mx_hosts', []),
    }


def _billable_count() -> int | None:
    """How many more addresses this run may bill, or None when nothing caps it.

    A Standby run is long-lived and serves many calls, but the pay-per-event
    limit (ACTOR_MAX_TOTAL_CHARGE_USD) applies to the *run*, not to the caller.
    Once it is spent the platform silently stops charging and then aborts the
    run, so asking first is what keeps this server from verifying addresses —
    and paying Verimailx for them — without being able to bill for the work.
    """
    try:
        return Actor.get_charging_manager().calculate_max_event_charge_count_within_limit(CHARGE_EVENT)
    except Exception as exc:  # noqa: BLE001 — no charging context (local run, non-PPE)
        Actor.log.debug(f'No charging limit available, treating as uncapped: {exc}')
        return None


async def _charge_one() -> bool:
    """Bill one verified address. False means the run's limit is now spent."""
    try:
        result = await Actor.charge(CHARGE_EVENT)
    except Exception as exc:  # noqa: BLE001 — never fail a verdict the caller already has
        Actor.log.warning(f'Charging {CHARGE_EVENT} failed: {exc}')
        return True
    return not result.event_charge_limit_reached


_LIMIT_HELP = (
    'This run has reached its pay-per-event spending limit, so no further addresses '
    'can be verified. Raise "Max total charge" for the Actor in Apify Console (or set '
    'maxTotalChargeUsd on the run) and try again.'
)


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

        Returns safe_to_send, an overall verdict of valid, invalid, risky or
        unknown, a deliverability score from 0 to 100, and the individual check
        results. Catch-all, disposable and role-based addresses are reported as
        risky, not valid — a catch-all domain accepts everything, so a positive
        result there proves nothing. Use this before adding an address to an
        outreach list or a CRM record.
        """
        if _billable_count() == 0:
            raise RuntimeError(_LIMIT_HELP)

        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            payload = await _post(client, VALIDATE_ENDPOINT, {'email': email})

        item = payload.get('result') if isinstance(payload.get('result'), dict) else payload
        shaped = _shape(item)
        if not await _charge_one():
            shaped['notice'] = _LIMIT_HELP
        return shaped

    @server.tool()
    async def verify_email_list(emails: list[str]) -> dict:
        """Verify a list of email addresses in one call.

        Same checks as verify_email, run concurrently. Duplicates are removed
        before verification. Accepts up to 25 addresses per call — for anything
        larger use the bulk Actor at
        apify.com/cold_email_master/bulk-email-verifier-validator, which takes a
        CSV link and handles tens of thousands of addresses.

        Returns one record per address, a summary count of each verdict, and how
        many addresses are safe to send to. An address that could not be checked
        comes back with an "error" field instead of a verdict and is not charged.
        """
        deduped = list(dict.fromkeys(e.strip().lower() for e in emails if e and e.strip()))

        if not deduped:
            raise ValueError('No email addresses were provided.')
        if len(deduped) > LIST_MAX:
            raise ValueError(
                f'{len(deduped)} addresses were provided but this tool accepts at most '
                f'{LIST_MAX}. For larger lists use the bulk Actor: {BULK_ACTOR_URL}'
            )

        # Trim to what this run can still bill *before* spending Verimailx credits
        # on addresses whose verdicts could never be charged for.
        budget = _billable_count()
        skipped: list[str] = []
        if budget is not None and budget < len(deduped):
            if budget <= 0:
                raise RuntimeError(_LIMIT_HELP)
            skipped = deduped[budget:]
            deduped = deduped[:budget]
            Actor.log.warning(
                f'Charge limit allows {budget} more address(es); '
                f'{len(skipped)} of this call were left unverified.'
            )

        gate = asyncio.Semaphore(LIST_CONCURRENCY)

        async def one(client: httpx.AsyncClient, address: str) -> dict:
            async with gate:
                try:
                    payload = await _post(client, VALIDATE_ENDPOINT, {'email': address})
                except Exception as exc:  # noqa: BLE001 — one bad address must not sink the batch
                    Actor.log.warning(f'{address} could not be verified: {exc}')
                    return {'email': address, 'safe_to_send': False, 'overall': 'unknown',
                            'verdict': None, 'error': str(exc)[:200]}
                item = payload.get('result') if isinstance(payload.get('result'), dict) else payload
                return _shape(item)

        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            shaped = await asyncio.gather(*(one(client, a) for a in deduped))

        # One charge call per address rather than a single batched call. Apify
        # recommends this: it keeps the caller's spending limit enforced per
        # unit, so a run that hits its cap stops cleanly instead of overshooting.
        # Only addresses that reached a verdict are billable.
        verified = [r for r in shaped if not r.get('error')]
        charged = 0
        limit_hit = False
        for _ in verified:
            charged += 1
            if not await _charge_one():
                limit_hit = True
                break

        summary: dict[str, int] = {}
        for record in shaped:
            summary[record['overall']] = summary.get(record['overall'], 0) + 1

        response = {
            'checked': len(shaped),
            'charged': charged,
            'safe_to_send': sum(1 for r in shaped if r['safe_to_send']),
            'summary': summary,
            'results': shaped,
        }
        if skipped:
            response['not_verified'] = skipped
        if limit_hit or skipped:
            response['notice'] = _LIMIT_HELP
        return response

    @server.custom_route('/', methods=['GET'])
    async def readiness(request: Request) -> PlainTextResponse:
        """Answer the platform's readiness probe.

        Before Standby routes any traffic to this container, Apify sends
        GET / carrying the x-apify-container-server-readiness-probe header, and
        a run that does not answer it is never marked ready — it would sit there
        logging a healthy startup while every MCP request went unserved. The MCP
        endpoint itself is mounted at /mcp, so without this route that probe gets
        a 404.
        """
        if request.headers.get('x-apify-container-server-readiness-probe'):
            return PlainTextResponse('ready')
        return PlainTextResponse(
            'Verimailx email verification MCP server. Connect an MCP client to /mcp.'
        )

    @server.resource(uri='resource://verimailx/info', name='verimailx-info')
    def info() -> str:
        """Describe what this MCP server does."""
        return (
            'Verimailx email verification. verify_email checks one address; '
            f'verify_email_list checks up to {LIST_MAX} at once. Each check runs RFC '
            'syntax, DNS, MX and SMTP-handshake validation and flags catch-all, '
            'disposable and role-based mailboxes, without sending any mail. Catch-all, '
            'disposable and role-based results are reported as risky rather than valid. '
            f'Billed per address verified. For larger lists use {BULK_ACTOR_URL}.'
        )

    return server


async def main() -> None:
    async with Actor:
        # Fail loudly at startup rather than answering the first tool call with a
        # configuration error — a Standby instance that cannot verify anything
        # should not sit there looking healthy.
        _api_key()

        server = build_server()
        app = server.http_app(transport='streamable-http')

        config = uvicorn.Config(
            app,
            host='0.0.0.0',  # noqa: S104 — required so the platform can route to the container
            port=Actor.configuration.web_server_port,
        )
        web_server = uvicorn.Server(config)
        server_task = asyncio.create_task(web_server.serve())

        # The platform sends `aborting` and force-stops 30 seconds later — on idle
        # timeout, on abort, or when the run's charge limit is spent. Letting
        # uvicorn drain in that window finishes in-flight tool calls instead of
        # dropping them as a broken connection on the caller's side.
        async def on_aborting(_event_data: object = None) -> None:
            Actor.log.info('Shutdown requested — draining in-flight requests.')
            web_server.should_exit = True

        Actor.on(Event.ABORTING, on_aborting)
        Actor.on(Event.MIGRATING, on_aborting)

        Actor.log.info(f'MCP server ready at {Actor.configuration.web_server_url}/mcp')

        # Serve until the platform shuts the Actor down — with Standby that happens
        # automatically once the instance has been idle past its timeout.
        await server_task


if __name__ == '__main__':
    asyncio.run(main())
