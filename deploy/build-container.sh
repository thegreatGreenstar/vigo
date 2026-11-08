#!/bin/sh
# Build both images Vigo's pod runs.
#
#   ./deploy/build-container.sh
#   VIGO_IMAGE=localhost/vigo:dev VIGO_AGENT_IMAGE=localhost/vigo-agent:dev ./deploy/build-container.sh
#
# The daemon image is minimal Debian.  The agent image is CachyOS on x86-64
# (this host's distribution, so builds from ~/Projects/compiled_scarlet run)
# and Arch Linux ARM on arm64 (CachyOS publishes no arm64 image, and the
# official `archlinux` image is x86-64 only, so it cannot start on the podman
# machine of an Apple silicon Mac).  Both fetch and check their own pinned
# OpenCode and gh.
set -eu

here=$(cd "$(dirname "$0")" && pwd)
if [ -f "$here/Containerfile" ]; then
    context=$here
else
    context=$here/container
fi
image=${VIGO_IMAGE:-localhost/vigo:latest}
agent_image=${VIGO_AGENT_IMAGE:-localhost/vigo-agent:latest}

case "$(uname -m)" in
x86_64|amd64) default_agent_base=docker.io/cachyos/cachyos:latest ;;
# The Arch Linux ARM image: the official Arch image has no arm64 manifest.
*)            default_agent_base=docker.io/menci/archlinuxarm:base ;;
esac
agent_base=${VIGO_AGENT_BASE:-$default_agent_base}

podman build --file "$context/Containerfile" --tag "$image" "$context"
podman build --file "$context/Containerfile.agent" \
    --build-arg "AGENT_BASE=$agent_base" --tag "$agent_image" "$context"
printf 'built %s and %s (%s)\n' "$image" "$agent_image" "$agent_base"
