# Pi Deployment Guide (spec M7)

Target: fresh Raspberry Pi 5 → running `tbctl-node` in **under 30 minutes**.
The Pi lives in the test cabinet on the bench LAN; the operator laptop is a
pure browser client (`http://192.168.10.10:8000`).

## Network plan

| Host | IP |
|---|---|
| Pi (`tbctl-node`) | `192.168.10.10/24` |
| LabJack T7 | `192.168.10.20/24` (static, matches `configs/channels.yaml`) |
| Operator laptop | `192.168.10.0/24` DHCP or static |

No gateway required on the bench LAN. **No auth in v1 (D6) — this network
must be an isolated bench LAN, never a shared office network.**

## 1. OS (5 min)

Flash Raspberry Pi OS Lite (64-bit) with Raspberry Pi Imager; preconfigure
hostname `tbctl`, user `tbctl`, and SSH in the imager. Boot and SSH in.

## 2. Static IP (2 min)

```bash
sudo nmcli con mod "Wired connection 1" \
  ipv4.method manual ipv4.addresses 192.168.10.10/24
sudo nmcli con up "Wired connection 1"
```

## 3. T7 static IP (one-time, 3 min)

With the T7 connected: use Kipling (any machine) to set
ETHERNET_IP_DEFAULT=192.168.10.20, subnet 255.255.255.0, DHCP off. Verify:
`ping 192.168.10.20`.

## 4. LJM library (3 min)

Install LabJack LJM for aarch64 (from labjack.com → LJM software,
`labjack_ljm_software_..._aarch64.deb` or tarball):

```bash
sudo dpkg -i labjack_ljm_software_*_aarch64.deb   # or run the tarball installer
ls /usr/local/share/LabJack/LJM/ljm_constants.json   # sanity check
```

## 5. tbctl (5 min)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh && source ~/.bashrc
sudo git clone <repo-url> /opt/tbctl && sudo chown -R tbctl:tbctl /opt/tbctl
cd /opt/tbctl
uv venv --python 3.12 && uv sync --extra hw
uv run tbctl validate --config-dir configs      # must print "all configs valid"
```

Frontend: copy a prebuilt `frontend/dist/` from CI or a dev machine, or
build on the Pi (adds ~5 min):

```bash
sudo apt install -y nodejs npm   # or use nvm for node 20+
cd frontend && npm ci && npm run build && cd ..
```

## 6. USB SSD data path (3 min)

```bash
lsblk                                   # identify the SSD, e.g. /dev/sda1
sudo mkfs.ext4 -L tbctl-data /dev/sda1  # first time only — ERASES THE DRIVE
sudo mkdir -p /data/tbctl
echo 'LABEL=tbctl-data /data/tbctl ext4 defaults,noatime,nofail 0 2' | sudo tee -a /etc/fstab
sudo systemctl daemon-reload && sudo mount -a
sudo mkdir -p /data/tbctl/runs /data/tbctl/logs
sudo chown -R tbctl:tbctl /data/tbctl
```

`nofail` keeps the Pi booting with the SSD absent; the node then fails at
run-start (not at boot), which is the desired failure mode.

## 7. Deploy the Lua interpreter + watchdog (2 min)

```bash
cd /opt/tbctl
uv run tbctl deploy-lua            # uploads lua/sequencer.lua, saves to flash,
                                   # verifies INTERP_VERSION, arms the hw watchdog
```

Re-run after every change to `lua/` or `shared/registers.yaml` (which also
requires bumping `interp_version` and re-running docs/timing-report.md).

## 8. systemd service (2 min)

```bash
sudo cp deploy/tbctl-node.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tbctl-node
systemctl status tbctl-node
curl -s localhost:8000/api/status | python3 -m json.tool | head
```

Open `http://192.168.10.10:8000` from the laptop — dashboard should show
live data and a green T7 chip.

## Simulator / training mode (--sim parity)

The exact same install runs hardware-free:

```bash
sudo systemctl stop tbctl-node          # don't fight over the port
cd /opt/tbctl && uv run tbctl-node --sim --data-dir /tmp/tbctl-runs
```

Everything works identically — streaming, recording, sequence load/arm/
fire/abort (against the simulated Lua), manual valves — because the mock
implements the full device surface (§9). Use it for operator training and
for UI work away from the bench. For a permanent training setup, add
`--sim` to the systemd `ExecStart`.

## Updating

```bash
cd /opt/tbctl && git pull
uv sync --extra hw
uv run tbctl codegen --check          # regenerate + redeploy Lua if this fails
uv run tbctl validate --config-dir configs
sudo systemctl restart tbctl-node
```

## Troubleshooting

- **`/api/status` shows device disconnected**: `ping 192.168.10.20`; check
  LJM install (`python -c "from labjack import ljm"`), check no second host
  holds the T7 connection.
- **`deploy-lua` fails**: it prints the `LUA_DEBUG` buffer — script syntax
  problems appear there. Never edit `lua/sequencer.lua` directly; edit
  `lua/sequencer_main.lua` and run `uv run tbctl codegen`.
- **interp version mismatch at node start**: re-run `uv run tbctl deploy-lua`.
- **No space / slow writes during runs**: confirm runs land on the SSD
  (`df /data/tbctl`), not the SD card.
