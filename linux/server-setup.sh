#!/bin/bash
# One-time setup that turns the SER9 Pro (or the Aero) into a quiet, always-on Ubuntu 24.04 server.
# It detects the graphics: AMD (SER9 Pro) or NVIDIA (Aero) and sets up the right one.
#   sudo bash linux/server-setup.sh <your-ntfy-topic>
# Safe to re-run. Every step prints what it does; nothing here touches your trading keys.
set -uo pipefail
[ "$(id -u)" = 0 ] || { echo "Run with sudo."; exit 1; }
TOPIC="${1:-}"
USER_NAME="${SUDO_USER:-$USER}"
step() { echo; echo "== $1"; }

timedatectl set-timezone America/Los_Angeles
step "Packages and automatic SECURITY updates (apps update only when you choose)"
apt-get update -q
apt-get install -y -q python3-venv python3-pip git curl smartmontools lm-sensors unattended-upgrades ufw nut-client
dpkg-reconfigure -f noninteractive unattended-upgrades   # Ubuntu's default config = security updates only
sed -i 's#^//\?\s*Unattended-Upgrade::Automatic-Reboot .*#Unattended-Upgrade::Automatic-Reboot "true";#' /etc/apt/apt.conf.d/50unattended-upgrades
grep -q 'Automatic-Reboot-Time' /etc/apt/apt.conf.d/50unattended-upgrades || \
  echo 'Unattended-Upgrade::Automatic-Reboot-Time "13:30";' >> /etc/apt/apt.conf.d/50unattended-upgrades  # weekday lunch, not night

step "Never sleep; ignore the lid; turn the screen off after 1 minute"
mkdir -p /etc/systemd/logind.conf.d
printf '[Login]\nHandleLidSwitch=ignore\nHandleLidSwitchExternalPower=ignore\nHandleLidSwitchDocked=ignore\n' > /etc/systemd/logind.conf.d/server.conf
systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target >/dev/null
if ! grep -q consoleblank /etc/default/grub; then
  sed -i 's/GRUB_CMDLINE_LINUX_DEFAULT="\(.*\)"/GRUB_CMDLINE_LINUX_DEFAULT="\1 consoleblank=60"/' /etc/default/grub && update-grub
fi

if lspci | grep -qi nvidia; then GPU=nvidia; else GPU=amd; fi
if [ "$GPU" = amd ]; then
step "AMD graphics (SER9 Pro): newer kernel for the Ryzen AI chip, GPU access for local AI"
apt-get install -y -q linux-generic-hwe-24.04 mesa-vulkan-drivers vulkan-tools
usermod -aG video,render "$USER_NAME"
else
step "NVIDIA driver 580 (last branch for the GTX 1060) + 80 W power cap"
if ! command -v nvidia-smi >/dev/null; then
  ubuntu-drivers install --gpgpu nvidia:580 || echo "  Driver install failed: run 'ubuntu-drivers list' and install the 580 branch by hand."
fi
cat > /etc/systemd/system/gpu-quiet.service <<UNIT
[Unit]
Description=Cap GPU power for quiet 24/7 running
After=multi-user.target
[Service]
Type=oneshot
ExecStart=/bin/sh -c 'nvidia-smi -pm 1; nvidia-smi -pl 80 || echo "power limit not adjustable on this GPU"'
[Install]
WantedBy=multi-user.target
UNIT
systemctl enable gpu-quiet.service >/dev/null
fi

step "SSD and drive health alerts to your phone (smartd)"
cat > /usr/local/bin/smart-ntfy <<SCRIPT
#!/bin/sh
[ -n "$TOPIC" ] && curl -s -H "Title: Disk warning" -H "Priority: high" -d "\$SMARTD_MESSAGE" "https://ntfy.sh/$TOPIC" >/dev/null
SCRIPT
chmod +x /usr/local/bin/smart-ntfy
echo "DEVICESCAN -a -n standby -s (S/../.././03|L/../../7/03) -m root -M exec /usr/local/bin/smart-ntfy" > /etc/smartd.conf
systemctl enable --now smartmontools >/dev/null 2>&1 || systemctl enable --now smartd >/dev/null 2>&1

step "Docker (+ GPU support for local AI)"
if ! command -v docker >/dev/null; then curl -fsSL https://get.docker.com | sh; fi
usermod -aG docker "$USER_NAME"
if [ "$GPU" = nvidia ] && ! dpkg -l | grep -q nvidia-container-toolkit; then
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
    sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
    > /etc/apt/sources.list.d/nvidia-container-toolkit.list
  apt-get update -q && apt-get install -y -q nvidia-container-toolkit && nvidia-ctk runtime configure --runtime=docker
  systemctl restart docker
fi

step "Free port 53 for AdGuard Home"
mkdir -p /etc/systemd/resolved.conf.d
printf '[Resolve]\nDNSStubListener=no\nDNS=1.1.1.1 9.9.9.9\n' > /etc/systemd/resolved.conf.d/adguard.conf
ln -sf /run/systemd/resolve/resolv.conf /etc/resolv.conf
systemctl restart systemd-resolved

step "Tailscale (private access from your iPhone/Mac, SSH, exit node)"
command -v tailscale >/dev/null || curl -fsSL https://tailscale.com/install.sh | sh
printf 'net.ipv4.ip_forward = 1\nnet.ipv6.conf.all.forwarding = 1\n' > /etc/sysctl.d/99-tailscale.conf && sysctl -p /etc/sysctl.d/99-tailscale.conf >/dev/null
tailscale up --ssh --advertise-exit-node || echo "  Open the login link above, then approve the exit node in the Tailscale admin page."

step "Firewall: nothing open to the internet; Tailscale + home network only"
ufw --force reset >/dev/null
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow in on tailscale0 >/dev/null
for NET in 192.168.0.0/16 10.0.0.0/8 172.16.0.0/12; do
  ufw allow from $NET to any port 53 >/dev/null      # AdGuard DNS for the house
  ufw allow from $NET to any port 445 proto tcp >/dev/null  # Time Machine
  ufw allow from $NET to any port 5353 proto udp >/dev/null # lets your Mac find the backup disk
  ufw allow from $NET to any port 3000 proto tcp >/dev/null # AdGuard admin page
done
ufw --force enable >/dev/null

echo
echo "Done. Reboot once (sudo reboot), then: bash linux/install.sh"
[ "$GPU" = amd ] && echo "SER9 Pro: in the BIOS (press Delete at power-on) set 'Power on after AC loss' to On, so it restarts after an outage."
echo "UPS: after you buy one, plug in its USB cable and see 'UPS' in linux/README.md."
