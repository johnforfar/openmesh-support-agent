# Openmesh CLI quick reference

This file is part of the support agent's local doc corpus. It is ingested
into pgvector at container startup and retrieved when users ask CLI questions.

## What is `om`?

`om` is the Openmesh CLI. It is a Rust binary that talks to the Xnode Manager
HTTP API on a sovereign Openmesh Xnode. With `om` you can deploy containerised
apps, expose them on public subdomains, manage the host NixOS configuration,
and inspect resource usage — all authenticated by your EVM wallet.

The full source lives at https://github.com/johnforfar/openmesh-cli.

## The four-step deploy pattern

Every app deploy on Openmesh follows the same shape:

```
deploy   →   wait   →   expose   →   verify
```

1. **deploy** — `om app deploy <name> --flake <uri>` builds the app as a
   NixOS container on the xnode.
2. **wait** — `om` polls `/request/<id>/info` until `nixos-rebuild switch`
   finishes. Default `--wait true` makes the deploy command block.
3. **expose** — `om app expose <name> --domain <fqdn> --port <n>` adds a
   reverse-proxy rule so the public can reach the container.
4. **verify** — `curl https://<fqdn>` or open it in a browser.

## Two-command deploy example

```bash
om app deploy support-agent \
  --flake github:johnforfar/openmesh-support-agent

om app expose support-agent \
  --domain chat.build.openmesh.cloud \
  --port 80
```

This is exactly how the support agent you are talking to right now was
deployed. No SSH, no manual NixOS edits, no web UI clicks.

## Command reference

### `om login --url <manager-url>`

Authenticate with the Xnode Manager. Stores a session cookie at
`~/.openmesh_session.cookie`. Re-run if you see `E_SESSION_EXPIRED`.

### `om wallet import` / `om wallet status` / `om wallet clear`

Manage your EVM wallet in the OS keychain (macOS Keychain or Linux Secret
Service). Private keys never touch disk.

### `om app list`

List all deployed containers on the xnode.

### `om app info <name>`

Show one container's flake configuration.

### `om app deploy <name> --flake <uri>`

Create or update a container. `--flake` accepts any flake URI:
`github:Openmesh-Network/xnode-apps?dir=jellyfin`,
`github:youruser/your-repo`, `gitlab:group/project`, etc.

Flags: `--update-input <name>` (repeatable), `--wait` (default true),
`--timeout <sec>` (default 600), `--dry-run`.

### `om app remove <name>`

Delete a container. Use `--wait false` to skip blocking.

### `om app expose <name> --domain <fqdn> --port <n>`

Add a public subdomain that forwards to a container's port.

Flags: `--protocol http|https|tcp|udp` (default http),
`--path <prefix>` (only forward requests under a path),
`--replace` (overwrite an existing rule for the same domain),
`--wait`, `--timeout`, `--dry-run`.

### `om app unexpose --domain <fqdn>`

Remove the reverse-proxy rule for a subdomain. Does not delete the container.

### `om req show <id>` / `om req wait <id>`

Inspect or block on the request id returned by every state-changing command.

### `om node info` / `om node status`

Show node config (domain, owner) and current resource usage.

## JSON mode

Every command supports `--format json`. Output is machine-readable.

```bash
om --format json app list
# {"containers": ["support-agent"]}

om --format json app deploy support-agent --flake github:johnforfar/openmesh-support-agent
# {"app": "support-agent", "request_id": 42, "status": "success"}
```

## Error codes (stable, branch on these from scripts and AI agents)

| Code | Meaning |
|---|---|
| `E_NOT_LOGGED_IN` | No session file. Run `om login`. |
| `E_SESSION_EXPIRED` | Manager returned 401. Run `om login` again. |
| `E_BAD_REQUEST` | Manager returned 4xx. Check your input. |
| `E_MANAGER_UNREACHABLE` | Network/TLS/5xx. Check connectivity. |
| `E_INVALID_RESPONSE` | Manager returned non-JSON. |
| `E_INVALID_INPUT` | A flag failed validation. |
| `E_NOT_FOUND` | Container or request id doesn't exist. |
| `E_ALREADY_EXISTS` | Pass `--replace` to overwrite. |
| `E_UNSAFE_FLAKE_EDIT` | flake editor refused to modify the host config. |
| `E_TIMEOUT` | Async op did not finish within `--timeout` seconds. |
| `E_INTERNAL` | Catch-all. |

## stdout vs stderr

Status messages go to **stderr**. Data goes to **stdout**. This means you
can pipe `om --format json ... | jq` and it stays parseable:

```bash
om --format json app list | jq '.containers[]'
```

## Available app templates

The official `Openmesh-Network/xnode-apps` repo has these templates ready
to deploy with `om app deploy`:

- `ollama` — local LLM inference (CPU or GPU)
- `jellyfin` — media server
- `nextcloud` — file sync and collaboration
- `immich` — self-hosted photo library
- `vaultwarden` — Bitwarden-compatible password manager
- `minecraft-server` — game server
- `vscode-server` — VS Code in the browser
- `near-validator` — NEAR blockchain validator
- `openclaw` — chat/agent gateway

Reference any of them as `github:Openmesh-Network/xnode-apps?dir=<name>`.

## Idempotent re-deploy from CI

`om app deploy` is `apply`-style: it creates if missing, updates if exists.
Safe to run from GitHub Actions on every push:

```yaml
- name: Deploy to xnode
  run: |
    om login --url ${{ secrets.XNODE_MANAGER_URL }}
    om --format json app deploy ${{ github.event.repository.name }} \
      --flake github:${{ github.repository }}
```

## Claude Code integration

Place an `OPENMESH-SKILLS.md` file in your project root. Claude Code will
read it on every session and know how to deploy your app via `om`. The
canonical version of OPENMESH-SKILLS.md lives in the openmesh-cli repo.
