# Removing a retired service/account from a repo

## 1. Start from fresh main in a worktree
The local checkout is often far behind the remote and may have untracked files. Branch from the remote instead:
`git fetch origin main && git worktree add ~/code/<repo>-wt-<name> -b <user>/remove-<name> origin/main`.
Tell the user the worktree path.

## 2. Grep with a word boundary and a column cap
Short names match inside other words (`cubi` matches `cubic-bezier` and `cubin`), and base64 fonts or images can dump megabytes into the output.
```bash
rg -n --pcre2 -M 300 '(?i)\b<name>(?![a-z])' --hidden -g '!.git' -g '!*lock*'
rg -n -M 300 '<account-id>|<zone-id>|<hostname>' --hidden -g '!.git' -l
```
Also grep the resource identifiers (account ID, hosted-zone ID, internal hostnames, secret prefixes), because many references never use the name.

## 3. Triage each hit
| Hit | Action |
|---|---|
| IaC grants/account maps (e.g. SSO assignments in Terraform) | Remove the entry and any merge/override that references it. Keep alignment fmt-clean. |
| Doc whose whole subject is hosted in the account | Delete the page and its index/README rows. Grep for links to it first. |
| Passing mention or example in a live doc or spec | Edit the sentence and reflow the paragraph. Fix any counts it changes ("5+ accounts" becomes "4"). |
| Code comments recording history | Drop the name and keep the lesson. |
| Attribution emails, arbitrary IDs used as test fixture data | Leave them, and list them in the PR as intentionally kept. |

## 4. Verify, commit, PR
- Re-run the grep. The only hits left should be the ones you kept on purpose.
- Run the repo's formatters on the touched files (e.g. `npx prettier@3 --check` on markdown, and `terraform fmt` if it is available).
- Use `git --no-pager diff` in scripts. A bare `git diff` inside a subprocess can block on the pager until it times out.
- Commit with the repo's co-author footer, then push and open the PR. The PR body should list deleted pages, edited files, intentionally kept hits, and a warning for any shared infra (DNS zone, forge, chat) that closure will break.
