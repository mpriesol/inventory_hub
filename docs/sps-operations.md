# Scoped SPS deployment operation — 18 November 2026

This operational branch uses the Hub repository's existing SSH connection only
to inspect the separately deployed SPS service. It does not modify Hub main,
deploy Hub images, run Hub migrations, restart Caddy or alter server permissions.
The workflow shares `inventory-hub-production` concurrency with ordinary releases.

SPS PR 1 is merged into `mpriesol/sps` main at
`f43a05831457254e150764547f5be2b4c984155f`. Its source tree passed the complete SPS
CI, including 105 backend tests, 25 frontend tests, Chromium workflows, PDF label
decoding and Docker persistence checks. Parcel photographs are bounded to 640 px
and 40 KiB before upload and again during server storage.

## Verified server state

The read-only operation verified the existing `/opt/sps` Compose deployment,
healthy container `sps-sps-1`, persistent volume `sps_courier_passwords`, and
networks `sps-proxy` / `sps_default`. Public `/sps/healthz` returned `ok`.

The configured SSH account cannot write the root-owned checkout or read its
`sps.env`. Its noninteractive administrator command failed because sudo requires
a password. No SPS replacement or server configuration change was performed.
Docker access is not used to circumvent these host-file permissions.

## Prepared administrator operation

The reviewed [deployment helper](../scripts/deploy_sps.py) accepts only
`--check <full-SPS-main-SHA>` or `--apply <full-SPS-main-SHA>`. Run it under the
existing authorized owner of `/opt/sps`; enter any sudo password only in the
server terminal, never in a workflow, command argument or chat.

It verifies the current SPS main SHA, clean tracked checkout, original Compose
files, data volume and proxy network. It builds from a Git archive of that exact
commit while the old service runs. After a graceful SPS stop it creates and
reads back a private backup of the entire data volume. It then replaces only SPS
and checks the exact image ID, volume, public health, HTML and frontend assets.
On replacement failure it restores the old image and retains all current data.
It never restores an older data backup automatically.

Private backups and a deployment lock live under the executing administrator's
`~/.local/state/sps-deploy`; no backup contents or configuration secrets are
uploaded to GitHub. Existing `sps.env`, Compose files, Hub and Caddy stay intact.
No photo-related Caddy limit increase is required for the small application
uploads. The helper refuses unexpected deployment layouts or local tracked edits.

Locally verified: Python syntax, clean diff, independent code review and mocked
success/build-failure/backup-failure/readiness-failure ordering. These checks do
not constitute a completed production deployment.
