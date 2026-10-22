# Safety validation — 2026-10-22

## Scope and observed results

Changes are based on the server's r6 staging sources, not a claim that the old GitHub main branch matched production. Source changes live on a separate maintenance branch. Production service files, configuration, orders and notifications were not changed. The pre-existing service remained active when inspected; it was not restarted.

Mac and Linux offline verification cover six suites: 27 original regression checks, 85 original fix checks, 8 decision-log cases, 18 entry/diagnostic cases, 12 callback cases, and 33 reconciliation/risk cases (183 checks/cases in total). Each suite uses fake configuration and a temporary HOME. The Linux run additionally uses a kernel network namespace without routes. Test counts are not counts of independent security properties.

The real server successfully read Gate public market data and its existing private account through authenticated GET requests. A temporary paper account completed a roundtrip using the observed public price; simulated fees and slippage produced a loss, as expected for an unchanged market. There was no available recent spot order among the queried pairs, so no actual historical fill was reconciled. The API adapter cannot send orders/cancellations and does not follow redirects. No real orders, cancellations, AI requests or WeChat messages were performed by the smoke check.

The real account check is deliberately not part of CI. It never exports credentials, balances, order IDs or signed headers. The checked credential file remained on the server and had owner-only 600 permissions.

## Behavior and limitations

- Live order transmission remains unavailable. A complete, separately authorized least-privilege sender and crash recovery around an actual send still need validation before any live mode can be considered. Reconciliation of existing orders is functional and persistent, but does not imply authorization to send new orders.
- Stable order snapshots and complete paginated trade evidence must agree on order ID, pair, side, account, base amount and quote total. The average comes from verified totals, not ticker price or Gate's deprecated `fill_price` field. Fee currencies, GT fees and point fees are preserved. Third-currency fees are not silently converted into a USDT profit number.
- Partial fills, zero-fill cancellations and unknown outcomes are distinct. Unknown intents remain unresolved; there is no automatic resubmit. Repeated fills are idempotent, including changed result ordering. Conflicting/mutated fills and regressing totals fail closed.
- Paper trades use Decimal and a single SQLite transaction for cash, FIFO costs, simulated fees, realized net PnL, protection state and idempotent results. The old mark-to-market-as-cash formula and latest-buy/full-quantity PnL formula were removed. Legacy migration validates but does not rewrite the old ledger. Unknown old fees remain a reporting limitation.
- The independent paper protection thread defaults to a 10-second wait between cycles. HTTP latency and unavailable data may extend the cycle or prevent exits; this is not exchange-hosted protection and cannot guarantee an execution price/loss limit. Fresh quotes for other positions may still trigger exits while one position's quote is missing. New buys require complete fresh portfolio quotes. Daily-loss/drawdown latches persist across restarts and days until an explicit reviewed state migration.
- Default paper assumptions: one-way fee/slippage 0.1% each, minimum simulated notional 5 USDT, position stop 5%, daily loss 3%, peak drawdown 10%, observation freshness 60 seconds. They are not live limits or actual account fee assertions. Freshness is based on local observation time, not proof of exchange quote creation time.
- The callback accepts only authenticated, authorized, fresh text messages for a self-built enterprise application. Its durable pre-dispatch replay claim provides at-most-once admission; a crash between claim and worker dispatch can lose the reply. A persistent outbox/rate-limited worker queue is still needed.
- Decision-log short raw secret detection, rotation concurrency, trusted ancestor directories and global LAST_ERROR concurrency retain the earlier disclosed limits. No production filesystem ACL/umask certification was performed.

## Security review

| Severity | Location (file:line) | Finding |
| --- | --- | --- |
| P2 | seven_roles_committee.py:409 | Provider response bodies and exception text could leak credentials through ordinary stdout; fixed to retain only HTTP status or exception type. A canary regression verifies both error paths. |

Review also confirmed a pre-existing remote-history exposure: `wechat_config.py` at commit `a152431` contained literal `SECRET`, `TOKEN`, `ENCODING_AES_KEY`, `DEEPSEEK_API_KEY` and `AI_API_KEY`. CORPID/AGENTID are identifiers. The maintenance branch removes current hardcoded credentials and excludes real client JSON/env/key files. A literal-value scan found none of those old secret values in the new working tree. This does not revoke credentials or remove public Git history. The exposed credentials need rotation at their providers; no history rewrite or credential rotation was performed here.

## Protocol sources

- [Gate spot API](https://www.gate.com/docs/developers/apiv4/en/spot/) — existing orders, personal trade pagination, filled amount/total and fee currencies.
- [Enterprise WeChat callback example](https://github.com/sbzhu/weworkapi_python/blob/master/callback/WXBizMsgCrypt.py) — signature/encryption envelope reference; live WeChat verification was not performed.

## Reproduction

Run `python tools/run_tests.py --json-output artifacts/offline-tests.json`. The runner copies Python sources, injects fake client configuration, clears credential environment variables and disables sockets for all children. It does not use any real client JSON in the checkout. For stronger isolation on Linux, run the entire runner under `unshare -n` after installing dependencies.

For read-only connectivity on a trusted machine, use `python tools/verify_runtime.py` and optionally `--env-file /path/to/600-permission/gateio.env`. This reads existing state and uses only a temporary paper/journal database. A successful result means read connectivity and the stated paper path worked; it is not proof of live trading or profitability.

Deployment preflight found an additional hardcoded SMTP authorization secret in the imported health_checker.py, missed by the earlier secret-name scan. It has been removed, SMTP configuration now comes only from environment variables, and sending is disabled unless explicitly enabled. The previous maintenance commits had already exposed this value, so it also requires provider-side revocation if not included in the user's completed rotation. No secret values are included in this note.

## Subsequent authorized paper rollout

A later explicitly authorized rollout updated the monitor in paper mode. The maintained suite now has 183 checks/cases. It did not enable real order transmission or notifications. Operational details and recovery records are retained privately. See production-rollout-20261022.md for the public release boundary.
