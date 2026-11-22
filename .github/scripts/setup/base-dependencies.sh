#!/usr/bin/env bash
#
# Install the host tools needed to build and run catalog-core apps
# (ubuntu-24.04: GCC 13, clang 18, QEMU 8.2).
#
# apt installs nothing if a single package is unavailable, so after
# installing we assert that every binary the test scripts need exists.
# A missing tool fails this step loudly instead of surfacing later as
# "unknown C compiler" or "Wrong message printed.".

set -euo pipefail

sudo apt-get -y update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
	build-essential \
	gcc-aarch64-linux-gnu \
	g++-aarch64-linux-gnu \
	libncurses-dev \
	libyaml-dev \
	flex \
	bison \
	git \
	wget \
	curl \
	uuid-runtime \
	qemu-system-x86 \
	qemu-system-arm \
	libarchive-tools \
	clang \
	lld \
	redis-tools \
	netcat-openbsd \
	iproute2 \
	iputils-ping

missing=0
for bin in gcc g++ clang aarch64-linux-gnu-gcc aarch64-linux-gnu-g++ \
	   qemu-system-x86_64 qemu-system-aarch64 bsdtar redis-cli \
	   netcat ip ping curl wget flex bison; do
	if ! command -v "$bin" > /dev/null 2>&1; then
		echo "::error::required tool '$bin' is not installed"
		missing=1
	fi
done
exit "$missing"
