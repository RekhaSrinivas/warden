# Real-time card authorization, live (14 Nov 2026)

Airwallex Issuing remote authorization pointed at Warden through a Cloudflare tunnel. Airwallex asked Warden
before each card charge; Warden answered from its Intent Receipts ("no Intent Receipt, no authorization").

The run used a copy of the 13 Nov live data, so this folder holds the whole book: the 13 Nov run's receipts plus
the 14 Nov ones at the end. The new entries:

| Charge | Warden | Airwallex card transaction event |
|---|---|---|
| research-bot, $12 at 7375, never approved by Warden (copied card details) | DECLINED in 0.1 ms | `01a1db32-2271-7000-a5e5-46e377ee34dc` · `REMOTE_AUTH_DECLINED` |
| travel-bot (hired from a plain-English job), United Airlines $180, approved by Warden first | AUTHORIZED in 0.1 ms | `01a1db59-ae09-7000-84fc-c14417e11bb6` · cleared |
| travel-bot, Marriott $135, approved by Warden first | AUTHORIZED in 0.1 ms | `01a1db59-e169-7000-921d-8cb74c131cd7` · cleared |
| travel-bot, $160 at 4511 with copied card details, within the card's own limits | DECLINED in 0.1 ms | `01a1db59-f739-7000-8656-e47cbd056e71` · `REMOTE_AUTH_DECLINED` |

Then two scam emails: a lookalike-domain invoice was blocked, and the fake bank-details email contained travel-bot
(card frozen). Look for `remote_authorization` events in `audit.jsonl`.

Check it:

```bash
python -m warden.receipts verify docs/live-runs/2026-11-14-remote-auth/receipts.jsonl docs/live-runs/2026-11-14-remote-auth/receipt-public-key.pem
# 43 receipts. Signatures and hash chain: intact
```

The receipt key is the same one as the 13 Nov run.
