# Outside facts used by `bot-messages-audit.html`

Checked 2026-10-27 against the official Telegram Bot API reference,
<https://core.telegram.org/bots/api> (latest changelog entry shown: Bot API 10.3,
2026-08-24).

| Fact | Where on the page | Used for |
|---|---|---|
| `sendMessage` text is "1-4096 characters after entities parsing" | sendMessage, parameter `text` | the bot's 4,000-character split |
| `disable_notification`: "Sends the message silently. Users will receive a notification with no sound." | sendMessage | calm milestones and spend notes sent silently |
| HTML parse mode supports `<pre>` (fixed-width block) and `<code>` | Formatting options, HTML style | variant B's ledger table |
| HTML parse mode supports `<blockquote>` and `<blockquote expandable>` ("Expandable block quotation") | Formatting options, HTML style | variant C's folded details |
| `expandable_blockquote` is a message entity type, "collapsed-by-default block quotation" | MessageEntity | variant C |
| `editMessageText` exists (the bot already uses it for the archive menu) | Available methods | one message updated in place instead of begin/done pairs and repeated alarms |

Not verified (no phone was used): how wide a `<pre>` block may be on a phone before
Telegram wraps or scrolls it. Variant B keeps every ledger line at 30 characters or
fewer for that reason.
