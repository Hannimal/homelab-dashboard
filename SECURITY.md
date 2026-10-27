# Security policy

## Reporting a vulnerability

Please report security problems privately through GitHub:
**Security → Report a vulnerability** on this repository. Don't open a public issue for them.

This is a small personal project, so expect a reply within a few days rather than hours.

## Scope and threat model

This is built for a trusted home network. What it does and doesn't protect:

- The Glances API on each server (`:61208`) has **no authentication**. The installer restricts it to the Pi's IP with `ufw` when `ufw` is active; without that, anyone on the network can read the stats. Do not expose it to the internet.
- The agent container mounts the Docker socket. Treat the agent as having control over Docker on that host, and keep the image pinned and updated.
- The dashboard on the Pi has no authentication either. It listens on `127.0.0.1` by default; only change `listen` if you accept that anyone who can reach the Pi can see your container names and IPs.
- The power menu (`--power`) can reboot and power off the Pi, behind the same PIN. Each use is logged to the journal (`journalctl -u homelab-dashboard`).
- Wi-Fi changes from the screen are opt-in (`--wifi`). They are guarded by a PIN stored in plain text in `config.json`, accept requests only from the Pi itself, and run the service as a fixed user that a polkit rule allows to scan and join Wi-Fi networks. Leave the feature off if you don't need it, and don't reuse a PIN you use elsewhere.
- `install.sh` runs code from this repository with `sudo`. Read it first, and pin `REPO_REF` to a release tag instead of `main` if you want reproducible installs.

See the "Security notes" section of the README for hardening details.
