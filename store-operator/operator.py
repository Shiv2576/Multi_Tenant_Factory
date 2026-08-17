# operator.py
import secrets as pysecrets

import kopf
import kubernetes.client as k8s

GROUP = "factory.example.com"
VERSION = "v1"
REGISTRY_NS = "factory-system"

ADMIN_PASSWORD = (
    "changeme123"  # fine for local/demo; move to a generated Secret before real use
)

# Per-STORE plan sizing (plan now lives on Store, not Tenant).
# requests/limits are what each store's containers actually ask for;
# the tenant's ResourceQuota is recomputed as the SUM of these across
# every Store that currently exists for that tenant.
PLAN_SIZES = {
    "basic": {
        "cpu_request_m": 150,
        "cpu_limit_m": 400,
        "mem_request_mi": 256,
        "mem_limit_mi": 512,
        "db_storage_mi": 512,
        "wp_storage_mi": 512,
    },
    "pro": {
        "cpu_request_m": 150,
        "cpu_limit_m": 600,
        "mem_request_mi": 256,
        "mem_limit_mi": 768,
        "db_storage_mi": 1024,
        "wp_storage_mi": 1024,
    },
}

# Hard cap for this portfolio demo: a tenant may deploy at most this many
# stores at once, in any mix of plans (e.g. 2 pro + 1 basic, or 3 basic).
MAX_STORES_PER_TENANT = 3


def _tenant_ns(tenant_id):
    return f"tenant-{tenant_id}"


def _recompute_quota(tenant_id, custom_api, core_v1, logger, exclude_store_name=None):
    """
    Reconciliation step: list every Store belonging to this tenant (optionally
    excluding one being deleted), sum up each one's plan footprint, and PATCH
    the tenant's ResourceQuota to match that sum exactly. This runs on every
    store create AND delete, so the quota always reflects reality rather than
    a one-time guess.
    """
    tenant_ns = _tenant_ns(tenant_id)
    all_stores = custom_api.list_namespaced_custom_object(
        group=GROUP, version=VERSION, namespace=REGISTRY_NS, plural="stores"
    )["items"]

    relevant = [
        s
        for s in all_stores
        if s["spec"]["tenantId"] == tenant_id
        and s["metadata"]["name"] != exclude_store_name
    ]

    total_cpu_m = 0
    total_mem_mi = 0
    total_storage_mi = 0
    total_pvcs = 0
    for s in relevant:
        plan = s["spec"]["plan"]
        size = PLAN_SIZES.get(plan, PLAN_SIZES["basic"])
        total_cpu_m += size["cpu_request_m"]
        total_mem_mi += size["mem_request_mi"]
        total_storage_mi += size["db_storage_mi"] + size["wp_storage_mi"]
        total_pvcs += 2  # one PVC for MySQL, one for WordPress, per store

    hard = {
        "requests.cpu": f"{total_cpu_m}m",
        "requests.memory": f"{total_mem_mi}Mi",
        "requests.storage": f"{total_storage_mi}Mi",
        "persistentvolumeclaims": str(total_pvcs),
    }

    try:
        core_v1.patch_namespaced_resource_quota(
            name="tenant-quota", namespace=tenant_ns, body={"spec": {"hard": hard}}
        )
        logger.info(
            f"Quota for '{tenant_ns}' recomputed -> {hard} ({len(relevant)} store(s))"
        )
    except k8s.exceptions.ApiException as e:
        if e.status != 404:
            raise
        logger.warning(f"No quota object found in '{tenant_ns}' yet to patch")

    return len(relevant)


# ============================================================
# TENANT handlers — the registration boundary. One per customer.
# ============================================================


@kopf.on.create(GROUP, VERSION, "tenants")
def on_tenant_created(spec, name, patch, logger, **kwargs):
    tenant_id = spec["tenantId"]
    tenant_ns = _tenant_ns(tenant_id)

    patch.status["phase"] = "Provisioning"
    core_v1 = k8s.CoreV1Api()
    networking_v1 = k8s.NetworkingV1Api()

    # --- Namespace ---
    ns_manifest = k8s.V1Namespace(
        metadata=k8s.V1ObjectMeta(
            name=tenant_ns, labels={"factory.example.com/tenant-id": tenant_id}
        )
    )
    try:
        core_v1.create_namespace(ns_manifest)
        logger.info(f"Namespace '{tenant_ns}' created")
    except k8s.exceptions.ApiException as e:
        if e.status != 409:
            raise
        logger.info(f"Namespace '{tenant_ns}' already exists")

    # --- ResourceQuota, starts at zero (no stores yet); grows as stores are added ---
    quota_manifest = k8s.V1ResourceQuota(
        metadata=k8s.V1ObjectMeta(name="tenant-quota", namespace=tenant_ns),
        spec=k8s.V1ResourceQuotaSpec(
            hard={
                "requests.cpu": "0m",
                "requests.memory": "0Mi",
                "requests.storage": "0Mi",
                "persistentvolumeclaims": "0",
            }
        ),
    )
    try:
        core_v1.create_namespaced_resource_quota(tenant_ns, quota_manifest)
        logger.info(f"ResourceQuota created for '{tenant_ns}' (starts empty)")
    except k8s.exceptions.ApiException as e:
        if e.status != 409:
            raise
        logger.info("ResourceQuota already exists")

    # --- NetworkPolicy: same-tenant pods + Traefik (kube-system) allowed in ---
    netpol_manifest = k8s.V1NetworkPolicy(
        metadata=k8s.V1ObjectMeta(name="deny-cross-tenant", namespace=tenant_ns),
        spec=k8s.V1NetworkPolicySpec(
            pod_selector=k8s.V1LabelSelector(),
            policy_types=["Ingress"],
            ingress=[
                k8s.V1NetworkPolicyIngressRule(
                    _from=[
                        k8s.V1NetworkPolicyPeer(
                            namespace_selector=k8s.V1LabelSelector(
                                match_labels={
                                    "factory.example.com/tenant-id": tenant_id
                                }
                            )
                        ),
                        k8s.V1NetworkPolicyPeer(
                            namespace_selector=k8s.V1LabelSelector(
                                match_labels={
                                    "kubernetes.io/metadata.name": "kube-system"
                                }
                            )
                        ),
                    ]
                )
            ],
        ),
    )
    try:
        networking_v1.create_namespaced_network_policy(tenant_ns, netpol_manifest)
        logger.info(f"NetworkPolicy created for '{tenant_ns}'")
    except k8s.exceptions.ApiException as e:
        if e.status != 409:
            raise
        logger.info("NetworkPolicy already exists")

    patch.status["phase"] = "Ready"
    patch.status["storeCount"] = 0


@kopf.on.delete(GROUP, VERSION, "tenants")
def on_tenant_deleted(spec, logger, **kwargs):
    """
    Deleting the Tenant ('the registry entry') tears down the WHOLE
    namespace unconditionally — every store the tenant ever created,
    regardless of plan, goes with it in one action.
    """
    tenant_id = spec["tenantId"]
    tenant_ns = _tenant_ns(tenant_id)
    core_v1 = k8s.CoreV1Api()
    try:
        core_v1.delete_namespace(tenant_ns)
        logger.info(
            f"Namespace '{tenant_ns}' deletion triggered (full tenant teardown)"
        )
    except k8s.exceptions.ApiException as e:
        if e.status != 404:
            raise
        logger.info(f"Namespace '{tenant_ns}' already gone")


# ============================================================
# STORE handlers — one per storefront. References a tenantId,
# chooses its own plan, gets storeId-suffixed resource names so
# many stores can coexist inside the same tenant namespace.
# ============================================================


@kopf.on.create(GROUP, VERSION, "stores")
def on_store_created(spec, name, patch, logger, **kwargs):
    tenant_id = spec["tenantId"]
    store_id = spec["storeId"]
    domain = spec["domain"]
    plan = spec["plan"]
    tenant_ns = _tenant_ns(tenant_id)
    size = PLAN_SIZES.get(plan, PLAN_SIZES["basic"])

    core_v1 = k8s.CoreV1Api()
    apps_v1 = k8s.AppsV1Api()
    batch_v1 = k8s.BatchV1Api()
    custom_api = k8s.CustomObjectsApi()

    def suffixed(base):
        return f"{base}-{store_id}"

    # --- Enforce the per-tenant store cap BEFORE provisioning anything ---
    existing = custom_api.list_namespaced_custom_object(
        group=GROUP, version=VERSION, namespace=REGISTRY_NS, plural="stores"
    )["items"]
    current_count = sum(
        1
        for s in existing
        if s["spec"]["tenantId"] == tenant_id and s["metadata"]["name"] != name
    )
    if current_count >= MAX_STORES_PER_TENANT:
        patch.status["phase"] = "Failed"
        logger.error(
            f"Tenant '{tenant_id}' already has {current_count} store(s) "
            f"(max {MAX_STORES_PER_TENANT}) — refusing to provision '{store_id}'"
        )
        return

    patch.status["phase"] = "Provisioning"

    # --- Recompute (grow) the tenant's quota to include this new store ---
    _recompute_quota(tenant_id, custom_api, core_v1, logger)

    # --- Secret for this store's MySQL root password ---
    db_password = pysecrets.token_urlsafe(16)
    secret_manifest = k8s.V1Secret(
        metadata=k8s.V1ObjectMeta(
            name=suffixed("mysql-credentials"), namespace=tenant_ns
        ),
        string_data={"MYSQL_ROOT_PASSWORD": db_password},
    )
    try:
        core_v1.create_namespaced_secret(tenant_ns, secret_manifest)
        logger.info(f"[{store_id}] MySQL credentials Secret created")
    except k8s.exceptions.ApiException as e:
        if e.status != 409:
            raise
        logger.info(f"[{store_id}] MySQL credentials Secret already exists")

    # --- Headless Service for MySQL ---
    svc_manifest = k8s.V1Service(
        metadata=k8s.V1ObjectMeta(name=suffixed("mysql"), namespace=tenant_ns),
        spec=k8s.V1ServiceSpec(
            cluster_ip="None",
            selector={"app": suffixed("mysql")},
            ports=[k8s.V1ServicePort(port=3306)],
        ),
    )
    try:
        core_v1.create_namespaced_service(tenant_ns, svc_manifest)
        logger.info(f"[{store_id}] Headless mysql Service created")
    except k8s.exceptions.ApiException as e:
        if e.status != 409:
            raise
        logger.info(f"[{store_id}] mysql Service already exists")

    # --- MySQL StatefulSet ---
    statefulset_manifest = k8s.V1StatefulSet(
        metadata=k8s.V1ObjectMeta(name=suffixed("mysql"), namespace=tenant_ns),
        spec=k8s.V1StatefulSetSpec(
            service_name=suffixed("mysql"),
            replicas=1,
            selector=k8s.V1LabelSelector(match_labels={"app": suffixed("mysql")}),
            template=k8s.V1PodTemplateSpec(
                metadata=k8s.V1ObjectMeta(labels={"app": suffixed("mysql")}),
                spec=k8s.V1PodSpec(
                    containers=[
                        k8s.V1Container(
                            name="mysql",
                            image="mysql:8.0",
                            args=[
                                "--innodb-buffer-pool-size=64M",
                                "--performance-schema=OFF",
                            ],
                            env=[
                                k8s.V1EnvVar(
                                    name="MYSQL_ROOT_PASSWORD",
                                    value_from=k8s.V1EnvVarSource(
                                        secret_key_ref=k8s.V1SecretKeySelector(
                                            name=suffixed("mysql-credentials"),
                                            key="MYSQL_ROOT_PASSWORD",
                                        )
                                    ),
                                ),
                                k8s.V1EnvVar(name="MYSQL_DATABASE", value="wordpress"),
                            ],
                            ports=[k8s.V1ContainerPort(container_port=3306)],
                            resources=k8s.V1ResourceRequirements(
                                requests={
                                    # MySQL gets ~65% of the store's request share, not
                                    # an even 50/50 — even with performance_schema off
                                    # and a capped buffer pool, MySQL's baseline memory
                                    # need is structurally higher than WordPress's.
                                    "cpu": f"{int(size['cpu_request_m'] * 0.65)}m",
                                    "memory": f"{int(size['mem_request_mi'] * 0.65)}Mi",
                                },
                                limits={
                                    "cpu": f"{int(size['cpu_limit_m'] * 0.65)}m",
                                    "memory": f"{int(size['mem_limit_mi'] * 0.65)}Mi",
                                },
                            ),
                            volume_mounts=[
                                k8s.V1VolumeMount(
                                    name="data", mount_path="/var/lib/mysql"
                                )
                            ],
                        )
                    ]
                ),
            ),
            volume_claim_templates=[
                k8s.V1PersistentVolumeClaim(
                    metadata=k8s.V1ObjectMeta(name="data"),
                    spec=k8s.V1PersistentVolumeClaimSpec(
                        access_modes=["ReadWriteOnce"],
                        storage_class_name="local-path",
                        resources=k8s.V1ResourceRequirements(
                            requests={"storage": f"{size['db_storage_mi']}Mi"}
                        ),
                    ),
                )
            ],
        ),
    )
    try:
        apps_v1.create_namespaced_stateful_set(tenant_ns, statefulset_manifest)
        logger.info(f"[{store_id}] MySQL StatefulSet created (plan={plan})")
    except k8s.exceptions.ApiException as e:
        if e.status != 409:
            raise
        logger.info(f"[{store_id}] MySQL StatefulSet already exists")

    # --- WordPress PVC ---
    wp_pvc_manifest = k8s.V1PersistentVolumeClaim(
        metadata=k8s.V1ObjectMeta(name=suffixed("wp-data"), namespace=tenant_ns),
        spec=k8s.V1PersistentVolumeClaimSpec(
            access_modes=["ReadWriteOnce"],
            storage_class_name="local-path",
            resources=k8s.V1ResourceRequirements(
                requests={"storage": f"{size['wp_storage_mi']}Mi"}
            ),
        ),
    )
    try:
        core_v1.create_namespaced_persistent_volume_claim(tenant_ns, wp_pvc_manifest)
        logger.info(f"[{store_id}] WordPress PVC created")
    except k8s.exceptions.ApiException as e:
        if e.status != 409:
            raise
        logger.info(f"[{store_id}] WordPress PVC already exists")

    # --- WordPress Deployment ---
    wp_env = [
        k8s.V1EnvVar(name="WORDPRESS_DB_HOST", value=suffixed("mysql")),
        k8s.V1EnvVar(name="WORDPRESS_DB_USER", value="root"),
        k8s.V1EnvVar(
            name="WORDPRESS_DB_PASSWORD",
            value_from=k8s.V1EnvVarSource(
                secret_key_ref=k8s.V1SecretKeySelector(
                    name=suffixed("mysql-credentials"), key="MYSQL_ROOT_PASSWORD"
                )
            ),
        ),
        k8s.V1EnvVar(name="WORDPRESS_DB_NAME", value="wordpress"),
    ]
    wp_deployment_manifest = k8s.V1Deployment(
        metadata=k8s.V1ObjectMeta(name=suffixed("wordpress"), namespace=tenant_ns),
        spec=k8s.V1DeploymentSpec(
            replicas=1,
            selector=k8s.V1LabelSelector(match_labels={"app": suffixed("wordpress")}),
            template=k8s.V1PodTemplateSpec(
                metadata=k8s.V1ObjectMeta(labels={"app": suffixed("wordpress")}),
                spec=k8s.V1PodSpec(
                    containers=[
                        k8s.V1Container(
                            name="wordpress",
                            image="wordpress:php8.2-apache",
                            env=wp_env,
                            ports=[k8s.V1ContainerPort(container_port=80)],
                            resources=k8s.V1ResourceRequirements(
                                requests={
                                    "cpu": f"{size['cpu_request_m'] - int(size['cpu_request_m'] * 0.65)}m",
                                    "memory": f"{size['mem_request_mi'] - int(size['mem_request_mi'] * 0.65)}Mi",
                                },
                                limits={
                                    "cpu": f"{size['cpu_limit_m'] - int(size['cpu_limit_m'] * 0.65)}m",
                                    "memory": f"{size['mem_limit_mi'] - int(size['mem_limit_mi'] * 0.65)}Mi",
                                },
                            ),
                            volume_mounts=[
                                k8s.V1VolumeMount(
                                    name="wp-data", mount_path="/var/www/html"
                                )
                            ],
                        )
                    ],
                    volumes=[
                        k8s.V1Volume(
                            name="wp-data",
                            persistent_volume_claim=k8s.V1PersistentVolumeClaimVolumeSource(
                                claim_name=suffixed("wp-data")
                            ),
                        )
                    ],
                ),
            ),
        ),
    )
    try:
        apps_v1.create_namespaced_deployment(tenant_ns, wp_deployment_manifest)
        logger.info(f"[{store_id}] WordPress Deployment created")
    except k8s.exceptions.ApiException as e:
        if e.status != 409:
            raise
        logger.info(f"[{store_id}] WordPress Deployment already exists")

    wp_svc_manifest = k8s.V1Service(
        metadata=k8s.V1ObjectMeta(name=suffixed("wordpress"), namespace=tenant_ns),
        spec=k8s.V1ServiceSpec(
            selector={"app": suffixed("wordpress")},
            ports=[k8s.V1ServicePort(port=80, target_port=80)],
        ),
    )
    try:
        core_v1.create_namespaced_service(tenant_ns, wp_svc_manifest)
        logger.info(f"[{store_id}] WordPress Service created")
    except k8s.exceptions.ApiException as e:
        if e.status != 409:
            raise
        logger.info(f"[{store_id}] WordPress Service already exists")

    # --- One-time Job: wp core install + activate WooCommerce ---
    setup_script = f"""
set -e
until wp core is-installed --path=/var/www/html --allow-root 2>/dev/null; do
  echo "WordPress not installed yet, attempting install..."
  wp core install --path=/var/www/html --allow-root \
    --url="{domain}" \
    --title="{store_id} ({plan})" \
    --admin_user=admin \
    --admin_password="{ADMIN_PASSWORD}" \
    --admin_email="admin@{domain}" && break
  echo "Install failed, likely DB not ready yet — retrying in 5s"
  sleep 5
done
echo "WordPress core install confirmed."

if wp plugin is-active woocommerce --path=/var/www/html --allow-root 2>/dev/null; then
  echo "WooCommerce already active."
else
  echo "Installing and activating WooCommerce..."
  wp plugin install woocommerce --activate --path=/var/www/html --allow-root
fi
"""
    job_manifest = k8s.V1Job(
        metadata=k8s.V1ObjectMeta(name=suffixed("wp-setup"), namespace=tenant_ns),
        spec=k8s.V1JobSpec(
            backoff_limit=4,
            template=k8s.V1PodTemplateSpec(
                spec=k8s.V1PodSpec(
                    restart_policy="OnFailure",
                    containers=[
                        k8s.V1Container(
                            name="wp-cli",
                            image="wordpress:cli-php8.2",
                            command=["sh", "-c", setup_script],
                            resources=k8s.V1ResourceRequirements(
                                requests={"cpu": "50m", "memory": "64Mi"},
                                limits={"cpu": "150m", "memory": "128Mi"},
                            ),
                            volume_mounts=[
                                k8s.V1VolumeMount(
                                    name="wp-data", mount_path="/var/www/html"
                                )
                            ],
                        )
                    ],
                    volumes=[
                        k8s.V1Volume(
                            name="wp-data",
                            persistent_volume_claim=k8s.V1PersistentVolumeClaimVolumeSource(
                                claim_name=suffixed("wp-data")
                            ),
                        )
                    ],
                )
            ),
        ),
    )
    try:
        batch_v1.create_namespaced_job(tenant_ns, job_manifest)
        logger.info(f"[{store_id}] WooCommerce setup Job created")
    except k8s.exceptions.ApiException as e:
        if e.status != 409:
            raise
        logger.info(f"[{store_id}] WooCommerce setup Job already exists")

    # --- cert-manager Certificate ---
    cert_manifest = {
        "apiVersion": "cert-manager.io/v1",
        "kind": "Certificate",
        "metadata": {"name": suffixed("tls-cert"), "namespace": tenant_ns},
        "spec": {
            "secretName": suffixed("tls-secret"),
            "dnsNames": [domain],
            "issuerRef": {"name": "selfsigned-local", "kind": "ClusterIssuer"},
        },
    }
    try:
        custom_api.create_namespaced_custom_object(
            group="cert-manager.io",
            version="v1",
            namespace=tenant_ns,
            plural="certificates",
            body=cert_manifest,
        )
        logger.info(f"[{store_id}] Certificate requested for '{domain}'")
    except k8s.exceptions.ApiException as e:
        if e.status != 409:
            raise
        logger.info(f"[{store_id}] Certificate already exists")

    # --- Traefik IngressRoute ---
    ingressroute_manifest = {
        "apiVersion": "traefik.io/v1alpha1",
        "kind": "IngressRoute",
        "metadata": {"name": suffixed("wordpress-route"), "namespace": tenant_ns},
        "spec": {
            "entryPoints": ["web", "websecure"],
            "routes": [
                {
                    "match": f"Host(`{domain}`)",
                    "kind": "Rule",
                    "services": [{"name": suffixed("wordpress"), "port": 80}],
                }
            ],
            "tls": {"secretName": suffixed("tls-secret")},
        },
    }
    try:
        custom_api.create_namespaced_custom_object(
            group="traefik.io",
            version="v1alpha1",
            namespace=tenant_ns,
            plural="ingressroutes",
            body=ingressroute_manifest,
        )
        logger.info(f"[{store_id}] IngressRoute created for '{domain}'")
    except k8s.exceptions.ApiException as e:
        if e.status != 409:
            raise
        logger.info(f"[{store_id}] IngressRoute already exists")

    patch.status["phase"] = "Ready"


@kopf.on.delete(GROUP, VERSION, "stores")
def on_store_deleted(spec, name, logger, **kwargs):
    """
    Deletes ONLY this store's resources (not the tenant namespace, not other
    stores), then shrinks the tenant's quota back down to match whatever
    stores remain.
    """
    tenant_id = spec["tenantId"]
    store_id = spec["storeId"]
    tenant_ns = _tenant_ns(tenant_id)

    core_v1 = k8s.CoreV1Api()
    apps_v1 = k8s.AppsV1Api()
    batch_v1 = k8s.BatchV1Api()
    custom_api = k8s.CustomObjectsApi()

    def suffixed(base):
        return f"{base}-{store_id}"

    deletions = [
        (
            custom_api.delete_namespaced_custom_object,
            dict(
                group="traefik.io",
                version="v1alpha1",
                namespace=tenant_ns,
                plural="ingressroutes",
                name=suffixed("wordpress-route"),
            ),
        ),
        (
            custom_api.delete_namespaced_custom_object,
            dict(
                group="cert-manager.io",
                version="v1",
                namespace=tenant_ns,
                plural="certificates",
                name=suffixed("tls-cert"),
            ),
        ),
        (
            batch_v1.delete_namespaced_job,
            dict(
                namespace=tenant_ns,
                name=suffixed("wp-setup"),
                propagation_policy="Background",
            ),
        ),
        (
            core_v1.delete_namespaced_service,
            dict(namespace=tenant_ns, name=suffixed("wordpress")),
        ),
        (
            apps_v1.delete_namespaced_deployment,
            dict(namespace=tenant_ns, name=suffixed("wordpress")),
        ),
        (
            core_v1.delete_namespaced_persistent_volume_claim,
            dict(namespace=tenant_ns, name=suffixed("wp-data")),
        ),
        (
            apps_v1.delete_namespaced_stateful_set,
            dict(namespace=tenant_ns, name=suffixed("mysql")),
        ),
        (
            core_v1.delete_namespaced_persistent_volume_claim,
            dict(namespace=tenant_ns, name=suffixed("data-mysql-0")),
        ),  # StatefulSet-generated PVC name
        (
            core_v1.delete_namespaced_service,
            dict(namespace=tenant_ns, name=suffixed("mysql")),
        ),
        (
            core_v1.delete_namespaced_secret,
            dict(namespace=tenant_ns, name=suffixed("mysql-credentials")),
        ),
    ]

    for fn, kwargs_ in deletions:
        try:
            fn(**kwargs_)
            logger.info(f"[{store_id}] deleted {kwargs_.get('name')}")
        except k8s.exceptions.ApiException as e:
            if e.status != 404:
                raise
            logger.info(f"[{store_id}] {kwargs_.get('name')} already gone")

    remaining = _recompute_quota(
        tenant_id, custom_api, core_v1, logger, exclude_store_name=name
    )
    logger.info(
        f"Store '{store_id}' torn down. Tenant '{tenant_id}' now has {remaining} store(s)."
    )
