---
name: attest
version: 1
capability: service.attest
role: service
protocol: hosted-api.v1
summary: Community-generated candidate for Attest; unclaimed, not provider-endorsed.
status: candidate
---
# Attest (candidate integration)

This SkillMD was generated from the provider's OpenAPI document. It is a claim, not a fact: nothing here has been executed or verified against the live service. Town tests and provider authorization are separate evidence.

## Operations

- GET /block (block_block_get): Block [declared effect: read]
- GET /claims (claims_claims_get): Claims [declared effect: read]
- POST /claims (file_claim_claims_post): File Claim [declared effect: write]
- POST /claims/{claim_id}/dispute (dispute_claims__claim_id__dispute_post): Dispute [declared effect: write]
- POST /claims/{claim_id}/resolve (resolve_claims__claim_id__resolve_post): Resolve [declared effect: write]
- GET /demo/agents (demo_agents_demo_agents_get): Demo Agents [declared effect: read]
- POST /demo/try (demo_try_demo_try_post): Demo Try [declared effect: write]
- POST /inbox/{receiver} (inbox_post_inbox__receiver__post): Inbox Post [declared effect: write]
- GET /inbox/{receiver}/get/{rid} (inbox_get_inbox__receiver__get__rid__get): Inbox Get [declared effect: read]
- GET /inbox/{receiver}/next (inbox_next_inbox__receiver__next_get): Inbox Next [declared effect: read]
- POST /inbox/{receiver}/reply (inbox_reply_inbox__receiver__reply_post): Inbox Reply [declared effect: write]
- GET /info (info_info_get): Info [declared effect: read]
- GET /pubkey (pubkey_pubkey_get): Pubkey [declared effect: read]
- GET /receipts (receipts_receipts_get): Receipts [declared effect: read]
- GET /receivers (receivers_receivers_get): Receivers [declared effect: read]
- POST /register/receiver (register_receiver_register_receiver_post): Register Receiver [declared effect: write]
- GET /replies/{agent_pub} (replies_replies__agent_pub__get): Replies [declared effect: read]
- POST /revoke (revoke_revoke_post): Revoke [declared effect: write]
- POST /sandbox/faucet (faucet_sandbox_faucet_post): Faucet [declared effect: write]
- POST /stake (stake_stake_post): Stake [declared effect: write]
- GET /stakes (stakes_stakes_get): Stakes [declared effect: read]
- GET /stats (stats_stats_get): Stats [declared effect: read]
- POST /verify/{receiver} (verify_verify__receiver__post): Verify [declared effect: write]

## Authentication

- Unknown. A reviewer must resolve how requests authenticate before any live test.

## Servers

- (none declared)

## Open questions for review

- no servers declared; the live endpoint is unknown
- no security schemes declared; how requests authenticate is unknown
- POST /claims is declared a write; its real side effects, costs, and permission rules are untested claims
- POST /claims/{claim_id}/dispute is declared a write; its real side effects, costs, and permission rules are untested claims
- POST /claims/{claim_id}/resolve is declared a write; its real side effects, costs, and permission rules are untested claims
- POST /demo/try is declared a write; its real side effects, costs, and permission rules are untested claims
- POST /inbox/{receiver} is declared a write; its real side effects, costs, and permission rules are untested claims
- POST /inbox/{receiver}/reply is declared a write; its real side effects, costs, and permission rules are untested claims
- POST /register/receiver is declared a write; its real side effects, costs, and permission rules are untested claims
- POST /revoke is declared a write; its real side effects, costs, and permission rules are untested claims
- POST /sandbox/faucet is declared a write; its real side effects, costs, and permission rules are untested claims
- POST /stake is declared a write; its real side effects, costs, and permission rules are untested claims
- POST /verify/{receiver} is declared a write; its real side effects, costs, and permission rules are untested claims
