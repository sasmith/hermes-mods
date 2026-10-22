---
name: cloud-service-decommission
description: Use when retiring a hosted service or cloud account. Shuts it down, stops billing, and removes its repo references.
version: 1.0.0
author: daveey
license: MIT
metadata:
  hermes:
    tags: [devops, billing, fly, decommission]
    related_skills: [compute-inventory-research, rent-machine, hermes-mods-repo]
---

# Cloud service decommission

For "we don't use X anymore, shut it down / make sure we aren't paying". The user wants it executed, not a menu. Pick defaults and proceed, then report gaps at the end.

## Procedure (in this order)

1. **Look for an existing retirement plan before touching anything.** Grep `~/.hermes/cron/jobs.json` for the provider/app name, and check the repo's `docs/plans/*sunset*` docs. A scheduled retirement job usually lists preconditions such as backup checks and a follow-up cleanup list. Follow it, because destroying first skips its safety gates. If you run the remaining steps early, rewrite the job (`cronjob_manage update`) to contain only those steps, then `run` it.
2. **Inventory everything billable on the account**, not just the named app: apps/services, machines, volumes, snapshots, dedicated IPs, certs, managed DBs/Redis/object storage, extensions. Provider commands are in `references/providers.md`.
3. **Confirm nothing live depends on it.** `dig` the custom domains to see whether they still resolve to this provider. Check where the service runs now (health endpoint on the replacement host). For a VM, inventory what actually runs on it before proposing anything (recipe in `references/providers.md`, "Any Linux VM"), and present a table: running thing → live/broken/obsolete → action. A VM often hosts a tunnel connector that fronts services on *other* machines, so read the tunnel's ingress rules and move every live hostname to another connector before destroying the VM.
   - Something unrelated to the request (a side project, a small web app) can be running on the same box. List it and ask keep-or-kill; archive its data locally before destroying.
4. **Verify the data is safe before you destroy anything.** Destroying an app usually deletes its volumes AND snapshots too. Check the replacement host's asset count against the latest off-site backup log or bucket. If a check fails, stop and report.
5. **Destroy** the resources, then re-list them and confirm zero remain.
6. **Revoke access paths** so nothing can recreate the service or keep a credential alive:
   - provider deploy tokens;
   - CI repo secrets (`gh secret list/delete -R owner/repo`);
   - deploy workflows: check the `main` branch via `gh api repos/O/R/contents/.github/workflows`, not just the local checkout, because local branches can be stale;
   - secrets stored in other systems (Cloudflare Secrets Store, local secret JSON files).
7. **Remove clutter**, such as stale VPN/WireGuard peers from CI runners. It's free, but it costs nothing to remove.
8. **Billing check.** Query the org's plan, billing status and credit balance via the provider API. The dashboard billing page may need a browser session. Tell the user if they have to remove the card or close the account by hand.
9. **Code/doc references** (config files, hardcoded URLs, docs, tests) are a separate, slower step. Hand it to a background job or subagent with a self-contained spec: run tests, push to main, confirm CI is green. If the user says the account or service is already being closed, this may be the only step requested. Run it directly and follow the repo's PR convention. The grep, triage and PR recipe is in `references/repo-reference-cleanup.md`.
10. **Report**: what was destroyed, what was revoked, the data-safety evidence, what is still running in the background, and any manual billing step. Admit any skipped gate up front.

## Pitfalls
- Read the docs you are about to delete for shared infrastructure that lives in the account, such as a DNS zone delegated from the main domain, a chat server or a git forge. Then `dig` those names. Closing the account takes them down for everyone, so warn the user before closure and don't just delete the doc quietly.
- Stopping or suspending an app does not stop volume or IP charges. Only destroying the resources ends billing.
- A one-shot cron job fired early with `run` keeps its original `next_run_at`. Remove it after the background run reports, or it fires again.
