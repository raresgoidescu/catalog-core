#!/usr/bin/env bash

set -euo pipefail

FIRECRACKER=v1.7.0

retry() {
	local n=$1
	shift
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

for tool in clang clang++; do
	sudo ln -sf "$(command -v "$tool")" "/usr/local/bin/aarch64-linux-gnu-$tool"
done

arch=$(uname -m)
retry 3 curl -fsSL -o /tmp/fc.tgz \
	"https://github.com/firecracker-microvm/firecracker/releases/download/${FIRECRACKER}/firecracker-${FIRECRACKER}-${arch}.tgz"
tar -xzf /tmp/fc.tgz -C /tmp
sudo install "/tmp/release-${FIRECRACKER}-${arch}/firecracker-${FIRECRACKER}-${arch}" "/usr/local/bin/firecracker-${arch}"

sudo mkdir -p /etc/qemu
echo "allow all" | sudo tee /etc/qemu/bridge.conf > /dev/null

missing=0
for bin in gcc g++ clang clang++ aarch64-linux-gnu-gcc aarch64-linux-gnu-g++ \
	aarch64-linux-gnu-clang aarch64-linux-gnu-clang++ \
	qemu-system-x86_64 qemu-system-aarch64 "firecracker-${arch}" \
	bsdtar redis-cli netcat ip ping curl wget flex bison docker; do
	if ! command -v "$bin" > /dev/null; then
		echo "::error::required tool '$bin' is not installed"
		missing=1
	fi
done
exit "$missing"
