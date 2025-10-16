#!/usr/bin/env bash

set -e

# Set up Docker Buildx
docker/setup-buildx-action@v3

# Enable Docker BuildKit
echo "DOCKER_BUILDKIT=1" >> $GITHUB_ENV
