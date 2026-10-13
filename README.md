# ansible-homelab-roles

Generic Ansible roles used to turn a freshly provisioned Proxmox guest (LXC or VM) into a hardened,
monitored host — and optionally give it its own MCP server so an AI agent can operate it through an
authenticated gateway.

The roles hold **no infrastructure data**: every address, user and key comes from your inventory.

| Role | What it does |
|---|---|
| `base` | Admin account with exclusive SSH keys, sshd hardening (keys only, no root, `AllowUsers`), nftables default-drop with an anti-lockout guard, unattended upgrades, timezone (LXC-safe), node_exporter, qemu-guest-agent on VMs |
| `mcp_shell` | Per-guest MCP server (FastMCP, streamable HTTP) exposing `run_command`, `read_file`, `write_file`, `system_info`. Reachable only from the gateway IP (nftables) **and** with a per-guest shared key (`X-MCP-Key`). Dedicated system user, sudo sessions recorded with `log_output`, every call logged to syslog (`mcp-shell-audit`) |

## Design notes

- **Firewall never touches Docker.** `base` resets only its own `inet filter` table (`table … / delete table … / table …`) instead of `flush ruleset`, and other roles drop rules into `/etc/nftables.d/*.nft`.
- **Anti-lockout.** `base` refuses to run without SSH keys and allowed sources, and always allows the IP of the controller currently connected.
- **Pinned dependencies.** `mcp_shell` pins `fastmcp` — an unpinned MCP SDK broke servers in the past.
- **Defense in depth for MCP.** OAuth is handled by the gateway; between gateway and guest, traffic is filtered by source IP *and* authenticated by a 48-char key generated on the controller (`ansible.builtin.password` lookup), never committed.

## Usage

```yaml
# requirements.yml
roles:
  - src: https://github.com/Raphaelmaury49/ansible-homelab-roles.git
    scm: git
    version: main
```

```yaml
# group_vars/all.yml (example values)
base_admin_user: admin
base_admin_ssh_keys:
  - ssh-ed25519 AAAA... you@laptop
base_ssh_allowed_sources: ["10.0.0.10/32"]
base_node_exporter_sources: ["10.0.0.20/32"]
mcp_shell_allowed_src: ["10.0.0.30"]
```

First run on a new guest connects as root (keys injected at provisioning), later runs as the admin user:

```bash
ansible-playbook site.yml --limit newhost -e ansible_user=root   # bootstrap
ansible-playbook site.yml --limit newhost --check --diff          # plan
```

Requires `ansible.posix` and `community.general`. Tested on Debian 13 (unprivileged LXC).

## License

MIT
