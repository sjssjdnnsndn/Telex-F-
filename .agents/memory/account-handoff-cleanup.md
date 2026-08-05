---
name: Account handoff cleanup
description: Safety rules for cleaning sold Telegram accounts and managing Telethon watcher lifecycles.
---

Account handoff cleanup must target Telegram's Saved Messages peer only (`me`), delete existing messages in batches, write the ownership notice after deletion, and verify that the notice is the only remaining message.

**Why:** The requested handoff behavior is destructive but must not affect normal chats, groups, channels, contacts, or other account data. Verification prevents partially cleaned accounts from entering inventory.

**How to apply:** Gate account onboarding and every single/bulk session or OTP delivery path on successful cleanup before charging or marking an account sold. Close Telethon watchers on success, failure, cancellation, and shutdown.