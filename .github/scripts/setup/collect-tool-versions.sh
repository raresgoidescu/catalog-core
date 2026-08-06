#!/usr/bin/env bash
#
# Run inside the `test` matrix job (which actually has qemu/firecracker/
# docker installed) and writes a small tool-versions.txt that gets
# bundled into that leg's artifact. The old generate-summary.sh tried to
# run `qemu-system-x86_64 --version` etc. from the *aggregate* job,
# which never installs any of these tools -- that's why the summary
# showed a blank QEMU version and a literal "command not found" for
# Firecracker. Versions are effectively identical across matrix legs
# (same base image, same setup scripts), so the aggregate job just picks
# whichever one it finds first.

set -e

{
  echo "QEMU Version: $(qemu-system-x86_64 --version 2>/dev/null | head -n1 || echo 'not available')"
  echo "Firecracker Version: $(firecracker-$(uname -m) --version 2>/dev/null | head -n1 || echo 'not available')"
  echo "Docker Version: $(docker --version 2>/dev/null || echo 'not available')"
  echo "GCC Version: $(gcc --version 2>/dev/null | head -n1 || echo 'not available')"
  echo "Clang Version: $(clang --version 2>/dev/null | head -n1 || echo 'not available')"
  echo "Runner: $(uname -a)"
} > tool-versions.txt

cat tool-versions.txt
