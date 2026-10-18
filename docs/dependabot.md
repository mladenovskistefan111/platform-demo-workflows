# Dependabot

`tools/gen-guardrails.py <repo> --dependabot-only` writes each repository's `.github/dependabot.yml`:

- **at most one pull request per repository per month**: a single multi-ecosystem group (language packages, Docker, Actions together), schedule monthly, 7-day cooldown;
- **major versions are ignored** everywhere, and Docker base images (Go, Node, Python, Temurin, ...) ignore **minor** versions as well: a toolchain or major upgrade is a deliberate decision (a card on the project board), not a bot PR;
- the rules are **in the file**, not in Dependabot's per-repository memory (`@dependabot ignore ...`), which a repository transfer wipes (that is what flooded the organisation with ~25 PRs after the move);
- Dependabot **security** alerts and security updates are a separate mechanism, enabled per repository by `tools/transfer-repo.sh` / `tools/protect-repo.sh`.

To take a major upgrade deliberately: raise it by hand (or temporarily remove the ignore), test, merge.
