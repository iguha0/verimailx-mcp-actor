# Email Verifier MCP Server — verify email addresses from Claude, Cursor or any agent

**Give your AI assistant the ability to check whether an email address is real.** This Actor is an [MCP](https://modelcontextprotocol.io) server: connect it once, and your agent can verify addresses mid-conversation — before it drafts outreach, enriches a CRM record, or hands you a lead list.

$0.30 per 1,000 addresses verified. No Verimailx account needed — you authenticate with your Apify token.

---

## What your agent can do

| Tool | What it does |
|---|---|
| `verify_email` | Check one address. Returns a verdict, a 0–100 deliverability score, and every individual check. |
| `verify_email_list` | Check up to 25 addresses in one call. Returns per-address records plus a summary count by verdict. |

Each check runs RFC syntax validation, DNS resolution, MX record inspection and an SMTP handshake, and flags disposable and role-based mailboxes. **No mail is ever sent** — the SMTP handshake asks the receiving server whether a mailbox exists and then disconnects, so nobody on your list is contacted.

Verdicts are `valid` (safe to send), `invalid` (will bounce), `risky` (disposable, role-based, or a catch-all domain) and `unknown` (server timeout or ambiguous response).

---

## Connect it

### Claude Desktop / Claude Code

Add this to your MCP configuration, using an [Apify API token](https://console.apify.com/account/integrations):

```json
{
  "mcpServers": {
    "verimailx": {
      "url": "https://cold-email-master--verimailx-email-mcp.apify.actor/mcp",
      "headers": {
        "Authorization": "Bearer <YOUR_APIFY_API_TOKEN>"
      }
    }
  }
}
```

### Cursor, Windsurf, and other MCP clients

Same URL, same bearer token. The server speaks MCP over the Streamable HTTP transport.

The Actor runs in [Standby mode](https://docs.apify.com/platform/actors/development/programming-interface/standby), so an instance is kept warm and answers immediately — there is no run to start.

---

## What it looks like in use

> **You:** Before I import these, are any of them going to bounce?
> `founder@stripe.com, info@mailinator.com, nobody@thisdomaindoesnotexist.xyz`
>
> **Claude:** *(calls `verify_email_list`)* One of three is safe to send to. `founder@stripe.com` is valid, score 98. `info@mailinator.com` is risky — it's a disposable address and a role mailbox. `nobody@thisdomaindoesnotexist.xyz` is invalid; the domain has no MX records.

---

## Pricing

**$0.30 per 1,000 addresses verified** — billed per address, not per call or per run.

| Volume | Cost |
|---|---:|
| 100 addresses | $0.03 |
| 1,000 addresses | $0.30 |
| 10,000 addresses | $3.00 |

Failed lookups are not charged. Apify's platform fee is included in the rate.

If you set a maximum charge for the run, the server checks the remaining budget
before it verifies anything. Once the budget is spent it says so plainly — it
will not keep verifying addresses it cannot bill you for, and a partial list
call tells you exactly which addresses it left unverified.

---

## Verifying more than 25 at a time

This server is built for conversational use — an agent checking a handful of addresses inside a task. For list cleaning at scale, use the bulk Actor instead: [Bulk Email Verifier & Validator](https://apify.com/cold_email_master/bulk-email-verifier-validator). It takes a link to a CSV, an Apify dataset ID, or a pasted list, and handles tens of thousands of addresses per run at the same price.

---

## FAQ

**Q: Do I need a Verimailx account?**
A: No. The Actor carries the Verimailx credentials; you authenticate with your Apify token and are billed through Apify.

**Q: Does this send email to the addresses I check?**
A: No. Verification stops at the SMTP handshake. No message is delivered and the recipient sees nothing.

**Q: How accurate is catch-all detection?**
A: Catch-all domains — ones configured to accept mail at any address — are reported as `risky` rather than `valid`, because an individual mailbox behind a catch-all cannot be confirmed. Treating them as valid is the most common cause of bounces on a "clean" list.

**Q: What happens to the addresses I check?**
A: They are sent to the Verimailx API for verification and are not written to any persistent storage by this Actor.

**Q: Can I use this without an AI agent?**
A: For scripted use the [REST Actor](https://apify.com/cold_email_master/bulk-email-verifier-validator) is a better fit — MCP is designed for tool-calling clients.

---

## Support

- **Issues with this Actor:** open an issue on the Actor's Issues tab
- **Bulk pricing (100k+ addresses):** contact us through Apify messaging
- **Verimailx API:** [verimailx.com](https://verimailx.com)

---

## Changelog

### 0.0.1 — Initial release
- `verify_email` and `verify_email_list` tools over Streamable HTTP, served through Actor Standby.
- Pay-per-event billing, charged per address verified.
