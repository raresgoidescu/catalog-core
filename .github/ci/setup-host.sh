#!/usr/bin/env bash
#
# Prepare a GitHub runner (ubuntu-24.04: GCC 13, clang 18, QEMU 8.2) to
# build and run catalog-core apps.
#
# apt installs nothing if a single package is unavailable, so at the end we
# check every tool the app scripts use: a missing one fails here, loudly,
# instead of later as "unknown C compiler" or "Wrong message printed.".

set -euo pipefail

FIRECRACKER=v1.7.0

retry() {   # retry <attempts> <cmd...>: for network hiccups only
	local n=$1; shift
	for i in $(seq "$n"); do
		"$@" && return 0
		echo "::warning::attempt $i/$n failed: $*"
		sleep $((i * 15))
	done
	return 1
}

retry 3 sudo apt-get -y update
retry 3 sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
	build-essential gcc-aarch64-linux-gnu g++-aarch64-linux-gnu \
	clang lld libncurses-dev libyaml-dev flex bison \
	git wget curl ca-certificates uuid-runtime libarchive-tools \
	qemu-system-x86 qemu-system-arm \
	redis-tools netcat-openbsd iproute2 iputils-ping

arch=$(uname -m)
retry 3 curl -fsSL -o /tmp/fc.tgz \
	"https://github.com/firecracker-microvm/firecracker/releases/download/${FIRECRACKER}/firecracker-${FIRECRACKER}-${arch}.tgz"
tar -xzf /tmp/fc.tgz -C /tmp
sudo install "/tmp/release-${FIRECRACKER}-${arch}/firecracker-${FIRECRACKER}-${arch}" "/usr/local/bin/firecracker-${arch}"

# Let QEMU attach instances to any bridge the run scripts create.
sudo mkdir -p /etc/qemu
echo "allow all" | sudo tee /etc/qemu/bridge.conf > /dev/null

missing=0
for bin in gcc g++ clang aarch64-linux-gnu-gcc aarch64-linux-gnu-g++ \
	   qemu-system-x86_64 qemu-system-aarch64 "firecracker-${arch}" \
	   bsdtar redis-cli netcat ip ping curl wget flex bison docker; do
	if ! command -v "$bin" > /dev/null; then
		echo "::error::required tool '$bin' is not installed"
		missing=1
	fi
done
exit "$missing"
