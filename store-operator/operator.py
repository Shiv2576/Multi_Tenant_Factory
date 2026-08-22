# operator.py
import secrets as pysecrets
import time

import kopf
import kubernetes.client as k8s

GROUP = "factory.example.com"
VERSION = "v1"
REGISTRY_NS = "factory-system"

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

MAX_STORES_PER_TENANT = 3


def _tenant_ns(tenant_id):
    return f"tenant-{tenant_id}"


SETUP_JOB_CPU_REQUEST_M = 50
SETUP_JOB_MEM_REQUEST_MI = 64
QUOTA_SAFETY_FACTOR = 1.15


def _recompute_quota(tenant_id, custom_api, core_v1, logger, exclude_store_name=None):
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
        total_cpu_m += size["cpu_request_m"] + SETUP_JOB_CPU_REQUEST_M
        total_mem_mi += size["mem_request_mi"] + SETUP_JOB_MEM_REQUEST_MI
        total_storage_mi += size["db_storage_mi"] + size["wp_storage_mi"]
        total_pvcs += 2

    total_cpu_m = int(total_cpu_m * QUOTA_SAFETY_FACTOR)
    total_mem_mi = int(total_mem_mi * QUOTA_SAFETY_FACTOR)

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
# TENANT handlers
# ============================================================


@kopf.on.create(GROUP, VERSION, "tenants")
def on_tenant_created(spec, name, patch, logger, **kwargs):
    tenant_id = spec["tenantId"]
    tenant_ns = _tenant_ns(tenant_id)

    patch.status["phase"] = "Provisioning"
    core_v1 = k8s.CoreV1Api()
    networking_v1 = k8s.NetworkingV1Api()

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


# ============================================================
# STORE handlers
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

    admin_username = spec.get("adminUsername") or "admin"
    admin_email = spec.get("adminEmail") or f"admin@{domain}"
    admin_password = spec.get("adminPassword") or pysecrets.token_urlsafe(12)
    public_url = f"https://{domain}/"
    admin_url = f"https://{domain}/wp-admin/"

    # ===== FIX: Wait for tenant namespace to exist =====
    logger.info(f"[{store_id}] Waiting for tenant namespace '{tenant_ns}' to exist...")
    for i in range(30):
        try:
            ns = core_v1.read_namespace(tenant_ns)
            if ns.status.phase == "Active":
                logger.info(f"[{store_id}] Tenant namespace '{tenant_ns}' is ready")
                break
        except k8s.exceptions.ApiException as e:
            if e.status == 404:
                logger.info(
                    f"[{store_id}] Namespace '{tenant_ns}' not found yet, waiting..."
                )
                time.sleep(2)
                continue
            else:
                raise
    else:
        patch.status["phase"] = "Failed"
        logger.error(f"[{store_id}] Timeout waiting for namespace '{tenant_ns}'")
        return

    # ===== FIX: Wait for ResourceQuota to exist =====
    logger.info(f"[{store_id}] Waiting for ResourceQuota in '{tenant_ns}'...")
    for i in range(30):
        try:
            quota = core_v1.read_namespaced_resource_quota("tenant-quota", tenant_ns)
            logger.info(f"[{store_id}] ResourceQuota exists in '{tenant_ns}'")
            break
        except k8s.exceptions.ApiException as e:
            if e.status == 404:
                logger.info(f"[{store_id}] ResourceQuota not found yet, waiting...")
                time.sleep(2)
                continue
            else:
                raise
    else:
        patch.status["phase"] = "Failed"
        logger.error(f"[{store_id}] Timeout waiting for ResourceQuota in '{tenant_ns}'")
        return

    # --- Enforce store cap ---
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

    # --- Recompute quota ---
    _recompute_quota(tenant_id, custom_api, core_v1, logger)

    # --- Secret for MySQL ---
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

    # --- WordPress PVC (wait for MySQL PVC to be bound) ---
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

    for i in range(30):
        try:
            pvc = core_v1.read_namespaced_persistent_volume_claim(
                name=f"data-{suffixed('mysql')}-0", namespace=tenant_ns
            )
            if pvc.status and pvc.status.phase == "Bound":
                logger.info(
                    f"[{store_id}] MySQL PVC is bound, creating WordPress PVC..."
                )
                break
        except k8s.exceptions.ApiException:
            pass
        time.sleep(2)

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
        k8s.V1EnvVar(
            name="WORDPRESS_CONFIG_EXTRA",
            value="""
$_SERVER['HTTPS'] = 'on';
if (isset($_SERVER['HTTP_X_FORWARDED_PROTO']) && $_SERVER['HTTP_X_FORWARDED_PROTO'] === 'https') {
    $_SERVER['HTTPS'] = 'on';
}
define('FORCE_SSL_ADMIN', true);
""",
        ),
    ]

    wp_deployment_manifest = k8s.V1Deployment(
        metadata=k8s.V1ObjectMeta(name=suffixed("wordpress"), namespace=tenant_ns),
        spec=k8s.V1DeploymentSpec(
            replicas=1,
            selector=k8s.V1LabelSelector(match_labels={"app": suffixed("wordpress")}),
            template=k8s.V1PodTemplateSpec(
                metadata=k8s.V1ObjectMeta(labels={"app": suffixed("wordpress")}),
                spec=k8s.V1PodSpec(
                    init_containers=[
                        k8s.V1Container(
                            name="copy-wordpress-files",
                            image="wordpress:php8.2-apache",
                            command=["sh", "-c"],
                            args=[
                                """
                                if [ ! -f /var/www/html/wp-load.php ]; then
                                    echo "Copying WordPress files to PVC..."
                                    cp -r /usr/src/wordpress/* /var/www/html/
                                    chown -R www-data:www-data /var/www/html
                                    echo "WordPress files copied successfully!"
                                else
                                    echo "WordPress files already exist on PVC."
                                fi
                                """
                            ],
                            # ===== ADD RESOURCES HERE =====
                            resources=k8s.V1ResourceRequirements(
                                requests={
                                    "cpu": "50m",
                                    "memory": "64Mi",
                                },
                                limits={
                                    "cpu": "100m",
                                    "memory": "128Mi",
                                },
                            ),
                            volume_mounts=[
                                k8s.V1VolumeMount(
                                    name="wp-data", mount_path="/var/www/html"
                                )
                            ],
                        )
                    ],
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

    # --- WordPress Service ---
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

    # --- Setup Job ---
    setup_script = """
set -e

echo "=== Starting WordPress setup ==="

echo "Waiting for WordPress files..."
for i in 1 2 3 4 5 6 7 8 9 10; do
    if [ -f /var/www/html/wp-load.php ]; then
    echo "✅ WordPress files found!"
    break
    fi
    echo "Waiting... $i/10"
    sleep 3
done

if [ ! -f /var/www/html/wp-load.php ]; then
    echo "❌ WordPress files not found! Exiting..."
    exit 1
fi

echo "Waiting for MySQL..."
# Use PHP's mysqli (same client WordPress itself uses) instead of the `mysql`
# CLI binary: this image's bundled MariaDB client can't complete a connection
# against MySQL 8's default caching_sha2_password + self-signed TLS setup
# (missing caching_sha2_password plugin, and rejects the self-signed cert).
# The connection check must be the loop's own condition — as a standalone
# statement before the loop, a failing first attempt trips `set -e` and kills
# the script before any retry ever runs.
until php -r '$m=@mysqli_connect(getenv("WORDPRESS_DB_HOST"), "root", getenv("MYSQL_ROOT_PASSWORD")); exit($m ? 0 : 1);'; do
    echo "Waiting for MySQL... (retry)"
    sleep 3
done
echo "✅ MySQL is ready!"

echo "Checking wp-config.php..."
if [ ! -f /var/www/html/wp-config.php ]; then
    echo "Creating wp-config.php..."
    # Use MYSQL_PWD environment variable instead of --pass to avoid escaping issues
    wp config create --path=/var/www/html --allow-root \
    --dbname=wordpress \
    --dbuser=root \
    --dbpass="$MYSQL_ROOT_PASSWORD" \
    --dbhost="$WORDPRESS_DB_HOST" \
    --skip-check

    # Add HTTPS settings
    echo 'if (isset($_SERVER["HTTP_X_FORWARDED_PROTO"]) && $_SERVER["HTTP_X_FORWARDED_PROTO"] === "https") { $_SERVER["HTTPS"] = "on"; }' >> /var/www/html/wp-config.php
    echo "define('FORCE_SSL_ADMIN', true);" >> /var/www/html/wp-config.php
    echo "✅ wp-config.php created"
else
    echo "✅ wp-config.php already exists"
fi

echo "Installing WordPress..."
if ! wp core is-installed --path=/var/www/html --allow-root 2>/dev/null; then
    wp core install --path=/var/www/html --allow-root \
    --url="$SITE_URL" \
    --title="$SITE_TITLE" \
    --admin_user="$ADMIN_USER" \
    --admin_password="$ADMIN_PASSWORD" \
    --admin_email="$ADMIN_EMAIL"
    echo "✅ WordPress installed!"
else
    echo "✅ WordPress already installed"
fi

echo "Installing WooCommerce..."
if ! wp plugin is-active woocommerce --path=/var/www/html --allow-root 2>/dev/null; then
    wp plugin install woocommerce --activate --path=/var/www/html --allow-root
    echo "✅ WooCommerce installed!"
else
    echo "✅ WooCommerce already active"
fi

echo "Seeding dummy products..."
EXISTING_PRODUCTS=$(wp post list --post_type=product --format=count --path=/var/www/html --allow-root)
if [ "$EXISTING_PRODUCTS" -eq "0" ]; then
    wp wc product create --path=/var/www/html --user="$ADMIN_USER" \
        --name="Sample Tee" --type=simple --regular_price="19.99" \
        --description="A comfortable everyday t-shirt." --allow-root
    wp wc product create --path=/var/www/html --user="$ADMIN_USER" \
        --name="Sample Mug" --type=simple --regular_price="12.50" \
        --description="A sturdy ceramic mug for your morning coffee." --allow-root
    wp wc product create --path=/var/www/html --user="$ADMIN_USER" \
        --name="Sample Tote Bag" --type=simple --regular_price="15.00" \
        --description="A durable canvas tote bag." --allow-root
    echo "✅ Dummy products created!"
else
    echo "✅ Products already exist ($EXISTING_PRODUCTS found), skipping seed"
fi

echo "Setting Shop page as homepage..."
# WooCommerce creates its "Shop" page on activation but does NOT make it the
# site's front page — without this, "/" shows WordPress's default page/blog
# and products are only visible by navigating to /shop directly.
SHOP_PAGE_ID=$(wp option get woocommerce_shop_page_id --path=/var/www/html --allow-root)
if [ -n "$SHOP_PAGE_ID" ] && [ "$SHOP_PAGE_ID" != "0" ]; then
    wp option update show_on_front page --path=/var/www/html --allow-root
    wp option update page_on_front "$SHOP_PAGE_ID" --path=/var/www/html --allow-root
    echo "✅ Shop page set as homepage!"
else
    echo "⚠️ Could not find WooCommerce shop page ID, leaving default homepage"
fi

echo "=== 🎉 Store setup complete! ==="
"""

    job_manifest = k8s.V1Job(
        metadata=k8s.V1ObjectMeta(name=suffixed("wp-setup"), namespace=tenant_ns),
        spec=k8s.V1JobSpec(
            backoff_limit=3,
            ttl_seconds_after_finished=300,
            template=k8s.V1PodTemplateSpec(
                spec=k8s.V1PodSpec(
                    restart_policy="OnFailure",
                    containers=[
                        k8s.V1Container(
                            name="wp-cli",
                            image="wordpress:cli-php8.2",
                            command=["sh", "-c", setup_script],
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
                                # These also match the WORDPRESS_DB_* names getenv_docker()
                                # reads from wp-config.php, in case the WordPress pod's
                                # entrypoint already generated that file on the shared PVC
                                # before this Job ran (skipping this script's own `wp config
                                # create` step).
                                k8s.V1EnvVar(
                                    name="WORDPRESS_DB_USER",
                                    value="root",
                                ),
                                k8s.V1EnvVar(
                                    name="WORDPRESS_DB_PASSWORD",
                                    value_from=k8s.V1EnvVarSource(
                                        secret_key_ref=k8s.V1SecretKeySelector(
                                            name=suffixed("mysql-credentials"),
                                            key="MYSQL_ROOT_PASSWORD",
                                        )
                                    ),
                                ),
                                k8s.V1EnvVar(
                                    name="WORDPRESS_DB_NAME",
                                    value="wordpress",
                                ),
                                k8s.V1EnvVar(
                                    name="WORDPRESS_DB_HOST",
                                    value=suffixed("mysql"),
                                ),
                                k8s.V1EnvVar(name="SITE_URL", value=public_url),
                                k8s.V1EnvVar(
                                    name="SITE_TITLE", value=f"{store_id} ({plan})"
                                ),
                                k8s.V1EnvVar(name="ADMIN_USER", value=admin_username),
                                k8s.V1EnvVar(
                                    name="ADMIN_PASSWORD", value=admin_password
                                ),
                                k8s.V1EnvVar(name="ADMIN_EMAIL", value=admin_email),
                            ],
                            # wordpress:php8.2-apache (Debian) and wordpress:cli-php8.2
                            # (Alpine) both have a "www-data" user, but at different
                            # numeric UIDs (33 vs 82). The PVC's files are owned by uid 33
                            # (created by the Apache image), so without pinning this
                            # container to the same uid/gid it can't write into
                            # wp-content/ (e.g. the WooCommerce plugin install fails).
                            security_context=k8s.V1SecurityContext(
                                run_as_user=33, run_as_group=33
                            ),
                            resources=k8s.V1ResourceRequirements(
                                # Request stays small so it fits the tenant's
                                # tight quota (only requests.* is quota-capped,
                                # not limits.*); the limit is raised well above
                                # it because installing WordPress core + the
                                # WooCommerce plugin zip needs real headroom —
                                # too tight a limit here causes a silent OOM
                                # kill mid-script with no error output.
                                requests={"cpu": "50m", "memory": "64Mi"},
                                limits={"cpu": "300m", "memory": "384Mi"},
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

    # --- Traefik IngressRoutes ---
    # NOTE: a single IngressRoute listing both "web" and "websecure" alongside a
    # top-level `tls` block causes Traefik to bind the plain-HTTP "web" router
    # into its TLS-only routing table, so it never matches unencrypted requests
    # (falls through to the default 404). Split into two IngressRoutes so HTTP
    # keeps working independently of the HTTPS/TLS one.
    http_ingressroute_manifest = {
        "apiVersion": "traefik.io/v1alpha1",
        "kind": "IngressRoute",
        "metadata": {"name": suffixed("wordpress-route-http"), "namespace": tenant_ns},
        "spec": {
            "entryPoints": ["web"],
            "routes": [
                {
                    "match": f"Host(`{domain}`)",
                    "kind": "Rule",
                    "services": [{"name": suffixed("wordpress"), "port": 80}],
                }
            ],
        },
    }
    https_ingressroute_manifest = {
        "apiVersion": "traefik.io/v1alpha1",
        "kind": "IngressRoute",
        "metadata": {"name": suffixed("wordpress-route-https"), "namespace": tenant_ns},
        "spec": {
            "entryPoints": ["websecure"],
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
    for ingressroute_manifest in (http_ingressroute_manifest, https_ingressroute_manifest):
        try:
            custom_api.create_namespaced_custom_object(
                group="traefik.io",
                version="v1alpha1",
                namespace=tenant_ns,
                plural="ingressroutes",
                body=ingressroute_manifest,
            )
            logger.info(
                f"[{store_id}] IngressRoute '{ingressroute_manifest['metadata']['name']}' created for '{domain}'"
            )
        except k8s.exceptions.ApiException as e:
            if e.status != 409:
                raise
            logger.info(
                f"[{store_id}] IngressRoute '{ingressroute_manifest['metadata']['name']}' already exists"
            )

    patch.status["phase"] = "Ready"
    patch.status["publicUrl"] = public_url
    patch.status["adminUrl"] = admin_url


@kopf.on.delete(GROUP, VERSION, "stores")
def on_store_deleted(spec, name, logger, **kwargs):
    """
    Deleting a Store CR only removes that CR — nothing else is owned by it
    (no ownerReferences), so without this handler every resource
    on_store_created made (MySQL, WordPress, PVCs, Job, Certificate,
    IngressRoutes) would keep running orphaned, unbilled and untracked by
    quota recompute, forever.
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

    logger.info(f"[{store_id}] Tearing down store resources in '{tenant_ns}'...")

    def _delete(label, fn):
        try:
            fn()
            logger.info(f"[{store_id}] Deleted {label}")
        except k8s.exceptions.ApiException as e:
            if e.status != 404:
                raise
            logger.info(f"[{store_id}] {label} already gone")

    _delete(
        f"IngressRoute '{suffixed('wordpress-route-https')}'",
        lambda: custom_api.delete_namespaced_custom_object(
            "traefik.io", "v1alpha1", tenant_ns, "ingressroutes",
            suffixed("wordpress-route-https"),
        ),
    )
    _delete(
        f"IngressRoute '{suffixed('wordpress-route-http')}'",
        lambda: custom_api.delete_namespaced_custom_object(
            "traefik.io", "v1alpha1", tenant_ns, "ingressroutes",
            suffixed("wordpress-route-http"),
        ),
    )
    _delete(
        f"Certificate '{suffixed('tls-cert')}'",
        lambda: custom_api.delete_namespaced_custom_object(
            "cert-manager.io", "v1", tenant_ns, "certificates", suffixed("tls-cert"),
        ),
    )
    _delete(
        f"Secret '{suffixed('tls-secret')}'",
        lambda: core_v1.delete_namespaced_secret(suffixed("tls-secret"), tenant_ns),
    )
    _delete(
        f"Job '{suffixed('wp-setup')}'",
        lambda: batch_v1.delete_namespaced_job(
            suffixed("wp-setup"), tenant_ns,
            propagation_policy="Background",
        ),
    )
    _delete(
        f"Service '{suffixed('wordpress')}'",
        lambda: core_v1.delete_namespaced_service(suffixed("wordpress"), tenant_ns),
    )
    _delete(
        f"Deployment '{suffixed('wordpress')}'",
        lambda: apps_v1.delete_namespaced_deployment(
            suffixed("wordpress"), tenant_ns,
            propagation_policy="Background",
        ),
    )
    _delete(
        f"PVC '{suffixed('wp-data')}'",
        lambda: core_v1.delete_namespaced_persistent_volume_claim(
            suffixed("wp-data"), tenant_ns,
        ),
    )
    _delete(
        f"StatefulSet '{suffixed('mysql')}'",
        lambda: apps_v1.delete_namespaced_stateful_set(
            suffixed("mysql"), tenant_ns,
            propagation_policy="Background",
        ),
    )
    # StatefulSet volumeClaimTemplates create their own PVC that survives the
    # StatefulSet's deletion by design (data safety) — must be removed explicitly.
    _delete(
        f"PVC '{f'data-{suffixed('mysql')}-0'}'",
        lambda: core_v1.delete_namespaced_persistent_volume_claim(
            f"data-{suffixed('mysql')}-0", tenant_ns,
        ),
    )
    _delete(
        f"Service '{suffixed('mysql')}'",
        lambda: core_v1.delete_namespaced_service(suffixed("mysql"), tenant_ns),
    )
    _delete(
        f"Secret '{suffixed('mysql-credentials')}'",
        lambda: core_v1.delete_namespaced_secret(
            suffixed("mysql-credentials"), tenant_ns,
        ),
    )

    # kopf's on.delete handler runs before the CR is actually removed (it holds
    # a finalizer until this returns), so the store being deleted is still
    # visible to _recompute_quota's own listing — exclude it explicitly.
    remaining = _recompute_quota(
        tenant_id, custom_api, core_v1, logger, exclude_store_name=name
    )
    logger.info(
        f"[{store_id}] Store teardown complete; {remaining} store(s) remain for '{tenant_ns}'"
    )


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
    apps_v1 = k8s.AppsV1Api()
    batch_v1 = k8s.BatchV1Api()

    logger.info(f"Starting full tenant teardown for '{tenant_ns}'...")

    # ===== FIX: Delete ALL resources in the namespace first =====
    try:
        # Delete all deployments
        deployments = apps_v1.list_namespaced_deployment(tenant_ns)
        for dep in deployments.items:
            apps_v1.delete_namespaced_deployment(dep.metadata.name, tenant_ns)
            logger.info(f"Deleted deployment: {dep.metadata.name}")
    except k8s.exceptions.ApiException as e:
        if e.status != 404:
            raise

    try:
        # Delete all statefulsets
        statefulsets = apps_v1.list_namespaced_stateful_set(tenant_ns)
        for sts in statefulsets.items:
            apps_v1.delete_namespaced_stateful_set(sts.metadata.name, tenant_ns)
            logger.info(f"Deleted statefulset: {sts.metadata.name}")
    except k8s.exceptions.ApiException as e:
        if e.status != 404:
            raise

    try:
        # Delete all jobs
        jobs = batch_v1.list_namespaced_job(tenant_ns)
        for job in jobs.items:
            batch_v1.delete_namespaced_job(
                job.metadata.name, tenant_ns, propagation_policy="Background"
            )
            logger.info(f"Deleted job: {job.metadata.name}")
    except k8s.exceptions.ApiException as e:
        if e.status != 404:
            raise

    try:
        # Delete all services
        services = core_v1.list_namespaced_service(tenant_ns)
        for svc in services.items:
            core_v1.delete_namespaced_service(svc.metadata.name, tenant_ns)
            logger.info(f"Deleted service: {svc.metadata.name}")
    except k8s.exceptions.ApiException as e:
        if e.status != 404:
            raise

    try:
        # ===== CRITICAL: Delete ALL PVCs =====
        pvcs = core_v1.list_namespaced_persistent_volume_claim(tenant_ns)
        for pvc in pvcs.items:
            # Remove finalizers from PVC
            try:
                core_v1.patch_namespaced_persistent_volume_claim(
                    pvc.metadata.name, tenant_ns, body={"metadata": {"finalizers": []}}
                )
            except:
                pass
            # Delete the PVC
            core_v1.delete_namespaced_persistent_volume_claim(
                pvc.metadata.name, tenant_ns
            )
            logger.info(f"Deleted PVC: {pvc.metadata.name}")
    except k8s.exceptions.ApiException as e:
        if e.status != 404:
            raise

    try:
        # Delete all secrets
        secrets = core_v1.list_namespaced_secret(tenant_ns)
        for secret in secrets.items:
            if secret.metadata.name.startswith("mysql-credentials"):
                core_v1.delete_namespaced_secret(secret.metadata.name, tenant_ns)
                logger.info(f"Deleted secret: {secret.metadata.name}")
    except k8s.exceptions.ApiException as e:
        if e.status != 404:
            raise

    # ===== Finally, delete the namespace =====
    try:
        # Remove finalizers from namespace
        core_v1.patch_namespace(tenant_ns, body={"metadata": {"finalizers": []}})
        # Delete the namespace
        core_v1.delete_namespace(tenant_ns)
        logger.info(f"Namespace '{tenant_ns}' deletion triggered")
    except k8s.exceptions.ApiException as e:
        if e.status != 404:
            raise
        logger.info(f"Namespace '{tenant_ns}' already gone")
