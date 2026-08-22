"""
Control Plane API for the Multi-Tenant Store Factory.

This service does NOT talk to operator.py directly — operator.py has no
HTTP interface at all. Instead, this API creates/reads/deletes the exact
same Tenant/Store custom resource objects you've been managing by hand
with `kubectl apply`. operator.py, running completely independently,
watches those objects and does the actual provisioning work.

    React Dashboard --HTTP--> this API --K8s API--> etcd --watched by--> operator.py

Auth: a tenant registers with a tenantId + password, gets back a JWT. Every
tenant/store endpoint derives which tenant is acting from that JWT's `sub`
claim — never from a client-supplied tenantId — so one tenant's token can
never be used to read or modify another tenant's stores.

Run locally against your cluster with:
    uv run uvicorn main:app --reload --port 8000

Requires a working kubeconfig (the same one `kubectl` uses).
"""

import base64
import hashlib
import os
import secrets
import time
from typing import Literal, Optional

import jwt
import kubernetes.client as k8s
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from kubernetes import config as k8s_config
from kubernetes.client.exceptions import ApiException
from pydantic import BaseModel, Field

GROUP = "factory.example.com"
VERSION = "v1"
REGISTRY_NS = "factory-system"
MAX_STORES_PER_TENANT = (
    3  # mirrors operator.py — enforced here too, for a fast UI error
)

# In production this must come from a real secret store, not a source-controlled
# default — but this whole factory has no such store yet, so fall back to a
# fixed dev value (with a loud warning) the same way the rest of the codebase
# already accepts plaintext defaults (e.g. mysql-credentials generation).
JWT_SECRET = os.environ.get("JWT_SECRET")
if not JWT_SECRET:
    JWT_SECRET = "insecure-dev-secret-set-JWT_SECRET-env-var-in-real-deployments"
    print(
        "WARNING: JWT_SECRET not set, using an insecure hardcoded dev default. "
        "Set the JWT_SECRET env var before exposing this API beyond localhost."
    )
JWT_ALGORITHM = "HS256"
JWT_TTL_SECONDS = 12 * 60 * 60  # 12h

app = FastAPI(title="Store Factory Control Plane API")

# Allow the local Vite dev server (and any dashboard origin you deploy) to call this API.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "*"
    ],  # tighten this to your dashboard's real origin before going further than local testing
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Kubernetes client setup ---
# Tries in-cluster config first (if this API is ever deployed AS a pod),
# falls back to the local kubeconfig (~/.kube/config) for local/dev use.
try:
    k8s_config.load_incluster_config()
except k8s_config.ConfigException:
    k8s_config.load_kube_config()

custom_api = k8s.CustomObjectsApi()
core_v1 = k8s.CoreV1Api()


# ============================================================
# Request/response models
# ============================================================


class AuthRequest(BaseModel):
    tenantId: str = Field(..., min_length=1, max_length=63, pattern=r"^[a-z0-9-]+$")
    password: str = Field(..., min_length=8, max_length=200)


class StoreCreateRequest(BaseModel):
    storeId: str = Field(..., min_length=1, max_length=63, pattern=r"^[a-z0-9-]+$")
    domain: str = Field(..., min_length=1)
    plan: Literal["basic", "pro"]
    adminUsername: Optional[str] = None
    adminEmail: Optional[str] = None
    adminPassword: Optional[str] = None


# ============================================================
# Auth helpers
# ============================================================
#
# Credentials are stored as a Kubernetes Secret per tenant (this system has no
# database — everything else already lives in the cluster, so credentials do
# too) instead of the Tenant CR itself, since CRs aren't meant to hold secrets
# and (unlike a Secret) aren't easily restricted from being read by anyone who
# can `kubectl get` the CRD.


def _auth_secret_name(tenant_id: str) -> str:
    return f"tenant-auth-{tenant_id}"


def _hash_password(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)


def _store_credentials(tenant_id: str, password: str) -> None:
    salt = secrets.token_bytes(16)
    digest = _hash_password(password, salt)
    # k8s Secret.string_data values get base64-encoded ONCE by the API server
    # on write (into .data). Store plain hex here — pre-base64-encoding it
    # ourselves would double-encode, and a single b64decode on read would
    # yield the hex string's bytes rather than the raw salt/hash.
    secret = k8s.V1Secret(
        metadata=k8s.V1ObjectMeta(
            name=_auth_secret_name(tenant_id), namespace=REGISTRY_NS
        ),
        string_data={"salt": salt.hex(), "hash": digest.hex()},
    )
    core_v1.create_namespaced_secret(REGISTRY_NS, secret)


def _verify_credentials(tenant_id: str, password: str) -> bool:
    try:
        secret = core_v1.read_namespaced_secret(
            _auth_secret_name(tenant_id), REGISTRY_NS
        )
    except ApiException as e:
        if e.status == 404:
            return False
        raise HTTPException(e.status, str(e))
    salt = bytes.fromhex(base64.b64decode(secret.data["salt"]).decode())
    expected = bytes.fromhex(base64.b64decode(secret.data["hash"]).decode())
    return secrets.compare_digest(_hash_password(password, salt), expected)


def _issue_token(tenant_id: str) -> str:
    payload = {"sub": tenant_id, "exp": int(time.time()) + JWT_TTL_SECONDS}
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def get_current_tenant_id(authorization: Optional[str] = Header(None)) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing or malformed Authorization header")
    token = authorization.removeprefix("Bearer ").strip()
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Token expired, log in again")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Invalid token")
    return payload["sub"]


# ============================================================
# Helpers
# ============================================================


def _tenant_object_name(tenant_id: str) -> str:
    return f"tenant-{tenant_id}"


def _store_object_name(tenant_id: str, store_id: str) -> str:
    return f"store-{tenant_id}-{store_id}"


def _get_tenant_or_404(tenant_id: str) -> dict:
    try:
        return custom_api.get_namespaced_custom_object(
            group=GROUP,
            version=VERSION,
            namespace=REGISTRY_NS,
            plural="tenants",
            name=_tenant_object_name(tenant_id),
        )
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(404, f"Tenant '{tenant_id}' is not registered")
        raise HTTPException(e.status, str(e))


# ============================================================
# Auth endpoints
# ============================================================


@app.post("/auth/register", status_code=201)
def register(req: AuthRequest):
    """Create a brand-new tenant: registers the Tenant CR (operator.py
    provisions its namespace/quota/network policy asynchronously) AND stores
    login credentials for it, then logs you straight in."""
    body = {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "Tenant",
        "metadata": {
            "name": _tenant_object_name(req.tenantId),
            "namespace": REGISTRY_NS,
        },
        "spec": {"tenantId": req.tenantId},
    }
    try:
        custom_api.create_namespaced_custom_object(
            group=GROUP,
            version=VERSION,
            namespace=REGISTRY_NS,
            plural="tenants",
            body=body,
        )
    except ApiException as e:
        if e.status == 409:
            raise HTTPException(409, f"Tenant '{req.tenantId}' already exists")
        raise HTTPException(e.status, str(e))

    try:
        _store_credentials(req.tenantId, req.password)
    except ApiException as e:
        # Don't leave an orphaned Tenant CR with no way to ever log into it.
        custom_api.delete_namespaced_custom_object(
            GROUP, VERSION, REGISTRY_NS, "tenants", _tenant_object_name(req.tenantId)
        )
        raise HTTPException(e.status, str(e))

    return {"token": _issue_token(req.tenantId), "tenantId": req.tenantId}


@app.post("/auth/login")
def login(req: AuthRequest):
    if not _verify_credentials(req.tenantId, req.password):
        raise HTTPException(401, "Invalid tenantId or password")
    return {"token": _issue_token(req.tenantId), "tenantId": req.tenantId}


# ============================================================
# Tenant endpoints
# ============================================================


@app.get("/tenants/me")
def get_tenant(tenant_id: str = Depends(get_current_tenant_id)):
    obj = _get_tenant_or_404(tenant_id)
    return {
        "tenantId": obj["spec"]["tenantId"],
        "phase": obj.get("status", {}).get("phase", "Pending"),
        "storeCount": obj.get("status", {}).get("storeCount", 0),
    }


@app.delete("/tenants/me", status_code=202)
def delete_tenant(tenant_id: str = Depends(get_current_tenant_id)):
    """Deletes the tenant AND every store it owns, in one action (operator.py
    tears down the whole namespace on tenant deletion)."""
    try:
        custom_api.delete_namespaced_custom_object(
            group=GROUP,
            version=VERSION,
            namespace=REGISTRY_NS,
            plural="tenants",
            name=_tenant_object_name(tenant_id),
        )
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(404, "Tenant not found")
        raise HTTPException(e.status, str(e))
    try:
        core_v1.delete_namespaced_secret(_auth_secret_name(tenant_id), REGISTRY_NS)
    except ApiException as e:
        if e.status != 404:
            raise HTTPException(e.status, str(e))
    return {"status": "deleting", "tenantId": tenant_id}


# ============================================================
# Store endpoints
# ============================================================


@app.get("/stores")
def list_stores(tenant_id: str = Depends(get_current_tenant_id)):
    """List all stores for the authenticated tenant, with live status."""
    items = custom_api.list_namespaced_custom_object(
        group=GROUP,
        version=VERSION,
        namespace=REGISTRY_NS,
        plural="stores",
    )["items"]
    result = []
    for s in items:
        if s["spec"]["tenantId"] != tenant_id:
            continue
        domain = s["spec"]["domain"]
        result.append(
            {
                "name": s["metadata"]["name"],
                "storeId": s["spec"]["storeId"],
                "domain": domain,
                "plan": s["spec"]["plan"],
                "phase": s.get("status", {}).get("phase", "Pending"),
                "adminUsername": s["spec"].get("adminUsername", "admin"),
                "publicUrl": s.get("status", {}).get(
                    "publicUrl", f"https://{domain}/"
                ),
                "adminUrl": s.get("status", {}).get(
                    "adminUrl", f"https://{domain}/wp-admin/"
                ),
            }
        )
    return result


@app.post("/stores", status_code=202)
def create_store(
    req: StoreCreateRequest, tenant_id: str = Depends(get_current_tenant_id)
):
    """Create a new store for the authenticated tenant. Fails fast (before
    hitting the cluster) if the tenant is already at the store cap — the same
    cap operator.py enforces, checked here too for instant UI feedback instead
    of a slow round-trip to Failed status."""
    _get_tenant_or_404(tenant_id)

    existing = custom_api.list_namespaced_custom_object(
        group=GROUP,
        version=VERSION,
        namespace=REGISTRY_NS,
        plural="stores",
    )["items"]
    current_count = sum(1 for s in existing if s["spec"]["tenantId"] == tenant_id)
    if current_count >= MAX_STORES_PER_TENANT:
        raise HTTPException(
            400,
            f"Tenant '{tenant_id}' already has {current_count} store(s) "
            f"(max {MAX_STORES_PER_TENANT})",
        )

    admin_username = req.adminUsername or "admin"
    admin_email = req.adminEmail or f"admin@{req.domain}"
    admin_password = req.adminPassword or secrets.token_urlsafe(12)

    body = {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "Store",
        "metadata": {
            "name": _store_object_name(tenant_id, req.storeId),
            "namespace": REGISTRY_NS,
        },
        "spec": {
            "tenantId": tenant_id,
            "storeId": req.storeId,
            "domain": req.domain,
            "plan": req.plan,
            "adminUsername": admin_username,
            "adminEmail": admin_email,
            "adminPassword": admin_password,
        },
    }
    try:
        custom_api.create_namespaced_custom_object(
            group=GROUP,
            version=VERSION,
            namespace=REGISTRY_NS,
            plural="stores",
            body=body,
        )
    except ApiException as e:
        if e.status == 409:
            raise HTTPException(
                409, f"Store '{req.storeId}' already exists for this tenant"
            )
        raise HTTPException(e.status, str(e))
    return {
        "status": "creating",
        "storeId": req.storeId,
        "publicUrl": f"https://{req.domain}/",
        "adminUrl": f"https://{req.domain}/wp-admin/",
        "adminUsername": admin_username,
        # Only ever returned here, at creation time — the CR isn't re-read for
        # this since there's no reason to make the password re-fetchable later.
        "adminPassword": admin_password,
    }


def _get_own_store_or_404(tenant_id: str, store_id: str) -> dict:
    try:
        s = custom_api.get_namespaced_custom_object(
            group=GROUP,
            version=VERSION,
            namespace=REGISTRY_NS,
            plural="stores",
            name=_store_object_name(tenant_id, store_id),
        )
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(404, "Store not found")
        raise HTTPException(e.status, str(e))
    # Belt-and-suspenders: the object name already embeds tenant_id, so this
    # can't actually mismatch, but never trust a spec field over the token.
    if s["spec"]["tenantId"] != tenant_id:
        raise HTTPException(404, "Store not found")
    return s


@app.get("/stores/{store_id}")
def get_store(store_id: str, tenant_id: str = Depends(get_current_tenant_id)):
    s = _get_own_store_or_404(tenant_id, store_id)
    domain = s["spec"]["domain"]
    return {
        "storeId": s["spec"]["storeId"],
        "domain": domain,
        "plan": s["spec"]["plan"],
        "phase": s.get("status", {}).get("phase", "Pending"),
        "adminUsername": s["spec"].get("adminUsername", "admin"),
        "publicUrl": s.get("status", {}).get("publicUrl", f"https://{domain}/"),
        "adminUrl": s.get("status", {}).get("adminUrl", f"https://{domain}/wp-admin/"),
    }


@app.delete("/stores/{store_id}", status_code=202)
def delete_store(store_id: str, tenant_id: str = Depends(get_current_tenant_id)):
    _get_own_store_or_404(tenant_id, store_id)
    custom_api.delete_namespaced_custom_object(
        group=GROUP,
        version=VERSION,
        namespace=REGISTRY_NS,
        plural="stores",
        name=_store_object_name(tenant_id, store_id),
    )
    return {"status": "deleting", "storeId": store_id}


@app.get("/health")
def health():
    return {"status": "ok"}
