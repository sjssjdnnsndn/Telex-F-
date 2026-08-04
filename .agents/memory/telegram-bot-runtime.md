---
name: Telegram bot runtime
description: Continuity and ownership rules for the Telegram polling bot and UPI API.
---

The Telegram bot must be the only process that long-polls the bot token; the UPI Node API may use the same token only for outbound notifications.

**Why:** Two concurrent polling consumers for one Telegram bot token cause update conflicts and unreliable bot behavior.

**How to apply:** Keep the bot as the only polling owner, keep `/ping` as a health endpoint only, and do not add an external keep-alive loop or a second polling client. Auto Scale is acceptable when the user explicitly chooses it, but it does not guarantee continuous background polling.