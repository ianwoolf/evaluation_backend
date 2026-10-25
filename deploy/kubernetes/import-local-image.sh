#!/usr/bin/env bash
# Local Docker Desktop kind node: Docker images and node containerd are separate.
set -euo pipefail
if (($# == 0)); then echo "Usage: $0 IMAGE:TAG [IMAGE:TAG ...]" >&2; exit 2; fi
for image in "$@"; do docker image inspect "$image" >/dev/null; done
if [[ -n "${KIND_CLUSTER_NAME:-}" ]]; then
  kind load docker-image --name "$KIND_CLUSTER_NAME" "$@"
  exit
fi
node_image="${KIND_NODE_IMAGE:-kindest/node:v1.36.1}"
docker image inspect "$node_image" >/dev/null
# Docker Desktop hides its kind node from normal docker ps. Enter only the
# containerd process whose root contains kubelet.conf. Ephemeral helper, no push.
docker save "$@" | docker run --rm -i --privileged --pid=host --entrypoint sh "$node_image" -c '
  for p in /proc/[0-9]*; do
    if test -f "$p/root/etc/kubernetes/kubelet.conf" && test "$(cat "$p/comm")" = containerd; then
      exec nsenter -t "${p##*/}" -m -p -- ctr -n k8s.io images import --all-platforms -
    fi
  done
  echo "Docker Desktop Kubernetes node containerd not found" >&2
  exit 1
'
