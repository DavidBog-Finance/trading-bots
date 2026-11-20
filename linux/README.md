# Home server: Beelink SER9 Pro (Ubuntu 24.04)

The SER9 Pro (32 GB) is the main server. The Aero still works with the same scripts (they detect
NVIDIA vs AMD graphics), but it is on the side for now.

1. Plug the SER9 into the router with Ethernet. In the BIOS (Delete at power-on), set "Power on after AC loss" to On.
2. Install **Ubuntu Server 24.04 LTS** from a USB stick (this erases Windows; copy anything you need first).
   Name the computer `ser9` when the installer asks, and tick "Install OpenSSH server".
3. `git clone` (or copy) this repo to `~/trading-bots`, then:
   ```bash
   sudo bash linux/server-setup.sh YOUR_NTFY_TOPIC   # sleep off, lid ignored, GPU driver + cap, Docker, Tailscale, firewall
   sudo reboot
   curl -fsSL https://claude.ai/install.sh | bash && claude   # log in with your Pro account, then /exit
   cp .env.example .env && nano .env                         # Alpaca keys, NTFY_TOPIC, CONTROL_TOPIC
   bash linux/install.sh                                     # bots service + home services
   ```
4. Turn off the Mac runner if it was on: `bash mac/uninstall.sh` on the Mac.

## Pages (open from your iPhone or Mac with Tailscale on; `ser9` = the server's Tailscale name)

| Page | Address |
|---|---|
| Private AI chat (Open WebUI) | http://ser9:3001 |
| Uptime Kuma (monitors) | http://ser9:3002 |
| Dockge (containers) | http://ser9:5001 |
| n8n (automations) | http://ser9:5678 |
| Miniflux (RSS) | http://ser9:8081 |
| Stirling-PDF | http://ser9:8082 |
| AdGuard Home | http://ser9:3000 (first run: set it up, then point your router's DNS at the SER9's home IP) |
| Time Machine | Mac: System Settings → General → Time Machine → Add Backup Disk → "TimeMachine" |

## Using it from your Mac and iPhone
- **Watch:** ntfy alerts and commands (`status`, `pause`, `kill`) as before; the pages above over Tailscale.
- **Talk to the local AI:** Open WebUI at http://ser9:3001 (add it to your iPhone home screen).
- **Prompt Claude Code on the server:** `ssh you@ser9` then `cd ~/trading-bots && claude`, from the Mac's
  Terminal or Termius on iPhone. Or start `claude remote-control` in that folder and drive it from the Claude app.

## Day to day
- Bot log: `sudo journalctl -u trading-bots -f` · restart: `sudo systemctl restart trading-bots`
- Health: `.venv/bin/python health.py` · data store: `.venv/bin/python datastore.py info`
- Scorecard now: `.venv/bin/python scorecard.py`
- SSH from anywhere: `ssh you@ser9` (Tailscale SSH), or the Termius app on iPhone.

## UPS
Plug the UPS's USB cable into the server, then `sudo nut-scanner -U` and follow its output into
`/etc/nut/ups.conf`; set `SHUTDOWNCMD "/sbin/shutdown -h now"` and a low-battery shutdown in `upsmon.conf`.
Most APC/CyberPower models work with the `usbhid-ups` driver. In the BIOS, enable "Restore on AC power loss" if offered.

## Quiet
- SER9 Pro: local AI runs on the built-in Radeon (Vulkan), or the CPU if that fails. Check with `docker logs ollama | grep -i vulkan`.
- Aero only: GPU capped at 80 W (`gpu-quiet` service). Ollama unloads the model after 10 idle minutes.
- Heavy jobs only 9 am–5 pm LA on weekdays (`HEAVY_HOURS`); news polling halves overnight (`QUIET_HOURS`).
- Security updates install automatically and, if a reboot is needed, it happens at 1:30 pm (after the market closes).
