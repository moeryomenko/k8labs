# Declarative Networking for declarek8s

This directory contains systemd-networkd and nftables configuration
files that replace the imperative bash scripts (`scripts/create-taps.sh`,
`scripts/destroy-taps.sh`) and iptables rules (in `Makefile`).

## Files

| File | Purpose |
|---|---|
| `k8sbr0.netdev` | Declares the `k8sbr0` Linux bridge. |
| `k8sbr0.network` | Assigns `192.168.124.1/24` to the bridge and runs a DHCP server (pool 192.168.124.20–200, 12h lease, domain `k8s.local`, static lease for Packer VM at .10). |
| `k8s-cp1.netdev` | Declares the `k8s-cp1` TAP device for the control-plane VM. |
| `k8s-cp1.network` | Enslaves `k8s-cp1` TAP to `k8sbr0` bridge. |
| `k8s-w1.netdev` | Declares the `k8s-w1` TAP device for the first worker VM. |
| `k8s-w1.network` | Enslaves `k8s-w1` TAP to `k8sbr0` bridge. |
| `packer-tap.netdev` | Declares the `packer-tap` TAP device for the Packer base-image build VM. |
| `packer-tap.network` | Enslaves `packer-tap` to `k8sbr0` bridge. |
| `dnsmasq-k8sbr0.conf` | dnsmasq DNS forwarder listening on the bridge address (192.168.124.1:53) for cluster VMs. |
| `nat.nft` | nftables ruleset: MASQUERADE for the VM subnet + FORWARD ACCEPT on `k8sbr0`. |

## How to Load

### systemd-networkd (bridge + TAPs + DHCP)

Copy the `.netdev` and `.network` files to `/etc/systemd/network/`:

```bash
sudo cp network/*.netdev network/*.network /etc/systemd/network/
sudo systemctl restart systemd-networkd
```

### nftables (NAT + forwarding)

```bash
sudo nft -f network/nat.nft
```

For persistence, include `nat.nft` in `/etc/nftables.conf` or copy it:

```bash
sudo cp network/nat.nft /etc/nftables.conf
sudo systemctl enable --now nftables
```

### DNS forwarding (dnsmasq)

The DHCP server hands out `192.168.124.1` as the DNS server, so the host
must run a DNS forwarder on the bridge address. Install the dnsmasq
config and start the service:

```bash
sudo mkdir -p /etc/dnsmasq.d
sudo cp network/dnsmasq-k8sbr0.conf /etc/dnsmasq.d/k8sbr0.conf
sudo systemctl enable --now dnsmasq
```

## Enslaving TAPs to the Bridge

The `.netdev` files create the TAP devices but do **not** automatically
attach them to `k8sbr0`. In systemd-networkd, bridge membership is
configured through `.network` files that match each TAP interface.

For each TAP, create a `.network` file like:

```ini
# k8s-cp1.network
[Match]
Name=k8s-cp1

[Network]
Bridge=k8sbr0
```

Copy to `/etc/systemd/network/` alongside the other files.

## Extending for More Workers

To add TAP devices for additional workers, create `k8s-w2.netdev`,
`k8s-w3.netdev`, etc. and matching `.network` files to enslave them
to the bridge.

## Requirements

- **systemd** v254+ (for `[DHCPServerStaticLease]` support)
- **nftables** v1.0.0+ (for `inet` family NAT support)
- Kernel features: `CONFIG_NFT_NAT`, `CONFIG_BRIDGE`, `CONFIG_TUN`
