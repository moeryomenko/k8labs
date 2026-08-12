# Declarative Networking for declarek8s

This directory contains systemd-networkd, nftables, and dnsmasq configuration
files that replace the imperative bash scripts (`scripts/create-taps.sh`,
`scripts/destroy-taps.sh`) and iptables rules (in `Makefile`).

## Files

| File | Purpose |
|---|---|
| `k8sbr0.netdev` | Declares the `k8sbr0` Linux bridge. |
| `k8sbr0.network` | Assigns `192.168.124.1/24` to the bridge and runs systemd-networkd's built-in DHCP server (pool 192.168.124.20–200, 12h lease, domain `k8s.local`, static lease for the Packer VM at .10). Declares the LB-pool route declaratively as `[Route] Destination=10.0.10.0/24` (`Scope=link`) — this replaced the former imperative `ip route add` in the Makefile, so the route survives networkd reloads and is removed cleanly on teardown. |
| `packer-tap.netdev` | Declares the `packer-tap` TAP device for the Packer base-image build VM. |
| `packer-tap.network` | Enslaves `packer-tap` to `k8sbr0` bridge. |
| `dnsmasq-k8sbr0.conf` | dnsmasq DNS forwarder for cluster VMs, bound only to the bridge address (192.168.124.1:53). Requires an active `conf-dir=/etc/dnsmasq.d/,*.conf` in `/etc/dnsmasq.conf` (see DNS forwarding). |
| `nat.nft` | Single dedicated nftables table `inet k8slab`: postrouting MASQUERADE for `192.168.124.0/24` + forward ACCEPT on `k8sbr0`. Idempotent load via the `destroy table inet k8slab` header; teardown is the scoped `sudo nft destroy table inet k8slab` — never a full ruleset flush, which would destroy foreign VPN tables such as `ip wg-quick-wg1`. |
| `90-k8slab-foreign-rules.conf` | networkd drop-in (`ManageForeignRoutingPolicyRules=no`). Installed permanently to `/etc/systemd/networkd.conf.d/` by `make network-up` and never removed by `make network-down`; protects wg-quick/VPN routing-policy rules from networkd cleanup. |

## How to Load

The canonical way to apply and tear down this configuration is through the
Makefile targets; they install the files above and keep the runtime state
consistent:

```bash
sudo make network-up    # install networkd files + drop-in, load k8slab nftables table,
                        # enable dnsmasq on the bridge (DHCP 192.168.124.20-200, DNS 192.168.124.1)
sudo make network-down  # remove networkd/dnsmasq configs, reload networkd,
                        # destroy the k8slab nftables table
```

`make network-up` is idempotent — re-running it never duplicates nftables
rules or the dnsmasq `conf-dir` activation. `make network-down` deliberately
keeps the `90-k8slab-foreign-rules.conf` drop-in installed (see
VPN / host connectivity safety).

### systemd-networkd (bridge + TAPs + DHCP)

`make network-up` copies the `.netdev` and `.network` files to
`/etc/systemd/network/` and reloads networkd. DHCP is provided by
systemd-networkd's built-in server configured in `k8sbr0.network`.

### nftables (NAT + forwarding)

- Load (idempotent): `sudo nft -f network/nat.nft`. The file starts with
  `destroy table inet k8slab`, so repeated loads replace the table instead of
  accumulating duplicate rules (verified: exactly 1 masquerade rule + 2
  forward accepts after repeated loads).
- Teardown (scoped): `sudo nft destroy table inet k8slab` — the command used
  by `make network-down`. It removes only the lab table. Never flush the whole
  ruleset: that would destroy foreign tables such as `ip wg-quick-wg1`
  (wg-quick's kill-switch / anti-spoof / connmark plumbing).
- Persistence: **runtime-only** by decision. The table is loaded by
  `make network-up` and torn down by `make network-down`; the
  `nftables.service` unit stays disabled and there is no
  `/etc/nftables.conf` persistence to install.

### DNS forwarding (dnsmasq)

The DHCP server hands out `192.168.124.1` as the DNS server, so the host runs
a dnsmasq DNS forwarder on the bridge address:

- Activation: `make network-up` copies `dnsmasq-k8sbr0.conf` to
  `/etc/dnsmasq.d/k8sbr0.conf`, then idempotently activates
  `conf-dir=/etc/dnsmasq.d/,*.conf` in `/etc/dnsmasq.conf` (uncomments the
  stock line or appends it, skipping if already active; refuses if an active
  `conf-dir` points elsewhere) before restarting dnsmasq. Without this
  activation the drop-in is inert.
- Binding: the drop-in uses `bind-dynamic` (not `bind-interfaces`) so dnsmasq
  tolerates `k8sbr0` appearing after it starts (e.g., boot ordering). With
  `interface=k8sbr0` / `listen-address=192.168.124.1` it binds only
  `192.168.124.1:53` — the wildcard `0.0.0.0:53` / `[::]:53` sockets are gone,
  closing the accidental LAN open resolver.

## VPN / host connectivity safety

This configuration is designed so the lab never disturbs host connectivity,
including a running wg-quick VPN (`wg1`):

- **Host NIC untouched**: the host NIC (`enp8s0`) is NetworkManager-managed;
  networkd reloads never bounce it.
- **No ruleset flush**: teardown removes only the `inet k8slab` table. Foreign
  tables (e.g., `ip wg-quick-wg1` with its kill-switch / anti-spoof / connmark
  rules) survive every load/teardown cycle.
- **Foreign policy rules protected**: the `90-k8slab-foreign-rules.conf`
  drop-in (`ManageForeignRoutingPolicyRules=no`) is installed permanently and
  stops networkd from stripping wg-quick's policy rules (e.g., the
  `not fwmark 0xca6c lookup 51820` and `suppress_prefixlength 0` rules) on
  reload — with or without the lab.
- **MASQUERADE is source-scoped**: the NAT rule matches `ip saddr
  192.168.124.0/24` only, so host traffic never hits it. The rule is
  deliberately not qualified on the output interface so VM egress can exit via
  `enp8s0` (direct) or via `wg1` (VPN full-tunnel) — whichever is the active
  route.

**VM egress traverses the VPN**: when `wg1` is up, VM traffic follows the
default route through the tunnel (no bypass rule is added) and is double-NAT'd
to the VPN endpoint; VM egress therefore depends on tunnel health. This is a
deliberate, documented behavior.

## Per-node TAPs

Per-node TAP devices — one `k8s-<node>.netdev` / `k8s-<node>.network` pair
per cluster node — are not committed here. `make nodes-generate` derives them
from `build/deploy.tfvars` into `build/network/`; `make network-up` installs
them to `/etc/systemd/network/` and removes any stale `k8s-*` pair left over
from a node that was dropped from tfvars. To add or remove a node's TAP, edit
the tfvars file and re-run `make network-up`. This directory holds only the
shared static configs listed above.

## Requirements

- **systemd** v254+ (for `[DHCPServerStaticLease]` support)
- **nftables** v1.0.0+ (for `inet` family NAT support)
- Kernel features: `CONFIG_NFT_NAT`, `CONFIG_BRIDGE`, `CONFIG_TUN`
