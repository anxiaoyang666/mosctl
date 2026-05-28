# Mosctl Panel Online Upgrade Design

## Goal

Add an online upgrade action for the Mosctl web panel itself. The existing MosDNS core upgrade remains unchanged.

## Scope

The panel upgrade updates only project-managed files:

- `/etc/mosdns/manager`
- `/usr/local/bin/mosctl`
- `/etc/mosdns/templates/default.yaml`
- Mosctl/MosDNS systemd unit files included in `remote-root`

It must not overwrite machine-local state:

- `/etc/mosdns/.env`
- `/etc/mosdns/config.yaml`
- `/etc/mosdns/rules`
- logs, cache, and runtime backups

## Flow

1. Read `MOSCTL_REPO_URL` and `MOSCTL_BRANCH` from `.env`, falling back to the public repository and `main`.
2. Download the selected GitHub branch archive, with the GitHub proxy as fallback.
3. Validate that the archive contains `remote-root` and that the new `app.py` compiles.
4. Back up current panel-managed files under `/etc/mosdns/backup/mosctl-panel.<timestamp>`.
5. Replace only the project-managed files.
6. Schedule a delayed `mosdns-web` restart so the API response can return before the process restarts.
7. If file replacement fails, restore the backup immediately.

## UI

Add a "Mosctl 面板升级" card beside the existing MosDNS core upgrade card. It shows repository and branch, has one refresh button for source settings, and one confirmation-protected upgrade button.

## Verification

Use source-level contract tests for the safety boundaries, Python compile checks for `app.py`, and remote service checks after deployment.
