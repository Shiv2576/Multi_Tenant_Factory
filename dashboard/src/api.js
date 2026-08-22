// api.js — thin wrapper around the Control Plane API (main.py / FastAPI).
// This talks ONLY to that API — never to operator.py, which has no HTTP
// interface at all.
//
// Every call except register/login sends the tenant's JWT as a Bearer token;
// the API derives which tenant is acting from that token alone, never from
// anything the client passes in the body/query — that's what keeps one
// tenant's session from ever touching another tenant's stores.

async function request(base, path, token, options = {}) {
  const headers = { "Content-Type": "application/json" };
  if (token) headers.Authorization = `Bearer ${token}`;
  const res = await fetch(`${base}${path}`, { headers, ...options });
  const isJson = res.headers.get("content-type")?.includes("application/json");
  const body = isJson ? await res.json() : null;
  if (!res.ok) {
    const message = body?.detail || `Request failed (${res.status})`;
    throw new Error(message);
  }
  return body;
}

export function createApi(base, token) {
  return {
    register: (tenantId, password) =>
      request(base, "/auth/register", null, {
        method: "POST",
        body: JSON.stringify({ tenantId, password }),
      }),

    login: (tenantId, password) =>
      request(base, "/auth/login", null, {
        method: "POST",
        body: JSON.stringify({ tenantId, password }),
      }),

    getTenant: () => request(base, "/tenants/me", token),

    deleteTenant: () => request(base, "/tenants/me", token, { method: "DELETE" }),

    listStores: () => request(base, "/stores", token),

    createStore: (
      storeId,
      domain,
      plan,
      adminUsername,
      adminEmail,
      adminPassword,
    ) =>
      request(base, "/stores", token, {
        method: "POST",
        body: JSON.stringify({
          storeId,
          domain,
          plan,
          adminUsername,
          adminEmail,
          adminPassword,
        }),
      }),

    deleteStore: (storeId) =>
      request(base, `/stores/${storeId}`, token, { method: "DELETE" }),
  };
}
