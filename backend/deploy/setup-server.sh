#!/bin/sh
# One-time setup for a fresh Oracle Cloud Ubuntu VM. Run on the server:
#   sh deploy/setup-server.sh
# Opens ports 80/443 in the VM's firewall, installs Docker, and lets the current user run it.
set -e

echo "==> Opening ports 80 and 443 in iptables"
sudo iptables -C INPUT -p tcp -m multiport --dports 80,443 -j ACCEPT 2>/dev/null \
  || sudo iptables -I INPUT -p tcp -m multiport --dports 80,443 -j ACCEPT
if command -v netfilter-persistent >/dev/null 2>&1; then
  sudo netfilter-persistent save
fi

echo "==> Updating packages"
sudo apt-get update -y
sudo DEBIAN_FRONTEND=noninteractive apt-get upgrade -y

if ! command -v docker >/dev/null 2>&1; then
  echo "==> Installing Docker"
  curl -fsSL https://get.docker.com | sudo sh
fi
sudo usermod -aG docker "$USER"

echo
echo "Done. Log out and back in (so the docker group applies), then:"
echo "  cd ~/backend"
echo "  cp .env.prod.example .env.prod && chmod 600 .env.prod && nano .env.prod"
echo "  docker compose -f docker-compose.prod.yml up -d --build"
