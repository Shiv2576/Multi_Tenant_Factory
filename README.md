uv run kopf run operator.py --namespace factory-system

problem faced in live store upgradation.

# Known Limitation: In-Place Store Plan Upgrades Are Not Supported

## What was attempted

An `on_store_updated` handler that would detect a `Store`'s `spec.plan`
changing (e.g. `basic` -> `pro`) and resize that store's running MySQL and
WordPress workloads in place, without deleting and recreating the store.

## Why it was dropped

CPU and memory resize in place is straightforward — patching a
StatefulSet's or Deployment's `resources.requests`/`resources.limits`
triggers a normal Pod restart with the new values, no different from
`kubectl edit`. That part works fine.

**Disk (PVC) resize in place does not work the same way, for two separate,
compounding reasons:**

### 1. The StorageClass must explicitly allow it

A PersistentVolumeClaim can only be resized live if its StorageClass has
`allowVolumeExpansion: true` set. This project uses `local-path`
(Rancher's default local-path-provisioner, used by both k3d locally and
plain k3s on a single-node VPS) — and **`local-path` does not support
volume expansion**. Attempting to `kubectl patch pvc ... -p
'{"spec":{"resources":{"requests":{"storage":"2Gi"}}}}'` against a
`local-path` PVC is rejected by the API server outright, regardless of
what the operator's code does.

The only common StorageClasses that _do_ support this are backed by real
CSI drivers with expansion support — Longhorn is one (used in the
original bare-metal/production design for this project, before the
local-VPS pivot to `local-path` for resource-cost reasons).

### 2. Even with an expandable StorageClass, a StatefulSet's volumeClaimTemplates are immutable

Separately from the StorageClass issue: a `StatefulSet`'s
`spec.volumeClaimTemplates` field is immutable after creation. Kubernetes
will reject any attempt to change it via `kubectl apply` or `patch` — you
cannot resize a StatefulSet-managed PVC's declared size through the
StatefulSet object itself, even if that PVC's underlying StorageClass
supports expansion. The correct approach (and one Kubernetes documents as
an intentional, manual escape hatch) is:

1. Edit the individual PVC object directly (not the StatefulSet) to
   request more storage.
2. Delete the StatefulSet **without** deleting its Pods
   (`kubectl delete statefulset ... --cascade=orphan`).
3. Recreate the StatefulSet with an updated `volumeClaimTemplates` size
   that matches the now-larger PVC.

This is a real, multi-step, manual-intervention process even in
production Kubernetes — not something a straightforward operator patch
can safely automate in a couple of API calls, especially for a stateful
database workload where getting the sequencing wrong risks data loss.

## The decision made instead

Given both constraints stack on top of each other, and given this project
is scoped as a portfolio/demo build with a hard cap of 3 stores per
tenant, in-place plan upgrades were dropped entirely. The supported
workflow to change a store's plan is:

```bash
kubectl delete store store-<tenantId>-<storeId> -n factory-system
# wait for teardown + quota shrink
kubectl apply -f - <<EOF
apiVersion: factory.example.com/v1
kind: Store
metadata:
  name: store-<tenantId>-<storeId>
  namespace: factory-system
spec:
  tenantId: <tenantId>
  storeId: <storeId>
  domain: <domain>
  plan: <new-plan>
EOF
```

This is simpler to reason about, simpler to demo, and avoids papering
over a limitation that is genuinely present in Kubernetes storage
mechanics — not specific to this project's code.

## If this were productionized beyond a portfolio demo

The clean fix would be switching the StorageClass from `local-path` to
one with `allowVolumeExpansion: true` (e.g. Longhorn, or a cloud
provider's block storage CSI driver), and implementing the
PVC-resize-then-StatefulSet-recreate sequence above as an explicit,
carefully-tested operator handler — treated as a genuinely higher-risk
operation than any other reconciliation path in this project, given it
touches live database storage.
# Multi_Tenant_Factory
