import { useEffect, useRef, useState } from "react";
import { createApi } from "./api";
import { EmptySlot, FilledSlot } from "./components/StoreSlot";
import { AddStoreForm } from "./components/AddStoreForm";
import { AuthPage } from "./components/AuthPage";

const MAX_STORES = 3;
const POLL_INTERVAL_MS = 3000;
const DEFAULT_API_BASE =
  localStorage.getItem("factory_api_base") || "http://localhost:8000";

export default function App() {
  const [apiBase, setApiBase] = useState(DEFAULT_API_BASE);
  const [token, setToken] = useState(localStorage.getItem("factory_token") || "");
  const [activeTenantId, setActiveTenantId] = useState(
    localStorage.getItem("factory_tenant_id") || ""
  );
  const [tenant, setTenant] = useState(null);
  const [stores, setStores] = useState([]);
  const [error, setError] = useState("");
  const [addingSlot, setAddingSlot] = useState(false);
  const [creatingStore, setCreatingStore] = useState(false);
  const [deletingStoreId, setDeletingStoreId] = useState(null);
  const [justCreated, setJustCreated] = useState(null);
  const [deletingTenant, setDeletingTenant] = useState(false);
  const pollRef = useRef(null);

  const isAuthed = Boolean(token && activeTenantId);
  const api = createApi(apiBase, token);

  useEffect(() => {
    localStorage.setItem("factory_api_base", apiBase);
  }, [apiBase]);

  useEffect(() => {
    if (!isAuthed) return;
    refresh();
    pollRef.current = setInterval(refresh, POLL_INTERVAL_MS);
    return () => clearInterval(pollRef.current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isAuthed, apiBase]);

  function handleAuthenticated(nextToken, tenantId) {
    localStorage.setItem("factory_token", nextToken);
    localStorage.setItem("factory_tenant_id", tenantId);
    setToken(nextToken);
    setActiveTenantId(tenantId);
  }

  function handleLogOut() {
    localStorage.removeItem("factory_token");
    localStorage.removeItem("factory_tenant_id");
    setToken("");
    setActiveTenantId("");
    setTenant(null);
    setStores([]);
  }

  async function handleDeleteTenant() {
    const confirmed = window.confirm(
      `Delete tenant '${activeTenantId}'? This destroys ALL ${stores.length} store(s) ` +
        "— MySQL, WordPress, PVCs, everything — permanently. This cannot be undone."
    );
    if (!confirmed) return;
    setDeletingTenant(true);
    setError("");
    try {
      await api.deleteTenant();
      handleLogOut();
    } catch (e) {
      setError(e.message);
    } finally {
      setDeletingTenant(false);
    }
  }

  async function refresh() {
    try {
      const [t, s] = await Promise.all([api.getTenant(), api.listStores()]);
      setTenant(t);
      setStores(s);
      setError("");
    } catch (e) {
      if (e.message.includes("Invalid token") || e.message.includes("expired")) {
        handleLogOut();
      } else {
        setError(e.message);
      }
    }
  }

  async function handleCreateStore({
    storeId,
    domain,
    plan,
    adminUsername,
    adminEmail,
    adminPassword,
  }) {
    setCreatingStore(true);
    setError("");
    try {
      const result = await api.createStore(
        storeId,
        domain,
        plan,
        adminUsername,
        adminEmail,
        adminPassword,
      );
      setJustCreated({ storeId, ...result });
      setAddingSlot(false);
      await refresh();
    } catch (e) {
      setError(e.message);
    } finally {
      setCreatingStore(false);
    }
  }

  async function handleDeleteStore(storeId) {
    setDeletingStoreId(storeId);
    setError("");
    try {
      await api.deleteStore(storeId);
      await refresh();
    } catch (e) {
      setError(e.message);
    } finally {
      setDeletingStoreId(null);
    }
  }

  if (!isAuthed) {
    return (
      <AuthPage
        apiBase={apiBase}
        setApiBase={setApiBase}
        onAuthenticated={handleAuthenticated}
      />
    );
  }

  const slots = Array.from({ length: MAX_STORES }, (_, i) => stores[i] || null);
  const atCapacity = stores.length >= MAX_STORES;

  return (
    <div className="shell">
      <aside className="rail">
        <div className="brand">
          <span className="brand-mark" />
          <span className="brand-title">STORE FACTORY</span>
        </div>
        <p className="brand-sub">tenant control panel</p>

        <div className="field">
          <label className="field-label">Control Plane API</label>
          <input
            value={apiBase}
            onChange={(e) => setApiBase(e.target.value)}
            placeholder="http://localhost:8000"
          />
        </div>

        <hr className="divider" />

        <div className="tenant-card">
          <div className="tenant-card-row">
            <span className="tenant-card-key">tenant</span>
            <span className="tenant-card-val">{activeTenantId}</span>
          </div>
          <div className="tenant-card-row">
            <span className="tenant-card-key">phase</span>
            <span className="tenant-card-val">{tenant?.phase || "—"}</span>
          </div>
          <div className="tenant-card-row">
            <span className="tenant-card-key">stores</span>
            <span className="tenant-card-val">
              {stores.length} / {MAX_STORES}
            </span>
          </div>
          <button className="btn btn-danger-ghost" onClick={handleLogOut}>
            Log out
          </button>
          <button
            className="btn btn-danger-ghost"
            onClick={handleDeleteTenant}
            disabled={deletingTenant}
          >
            {deletingTenant ? "Deleting tenant…" : "Delete tenant"}
          </button>
        </div>
      </aside>

      <main className="main">
        <div className="main-header">
          <div>
            <h1 className="main-title">{activeTenantId} — stores</h1>
            <p className="main-caption">
              Each tenant gets a fixed capacity of {MAX_STORES} stores, any mix of
              plans. Delete a store to free its slot.
            </p>
          </div>
          <div className="capacity-readout">
            CAPACITY
            <br />
            <b>
              {stores.length}/{MAX_STORES}
            </b>
          </div>
        </div>

        {error && <div className="error-banner">{error}</div>}

        {justCreated && (
          <div className="credentials-banner">
            <strong>{justCreated.storeId}</strong> created — save these now,
            the password won't be shown again:
            <div>
              Admin URL: <a href={justCreated.adminUrl} target="_blank" rel="noreferrer">{justCreated.adminUrl}</a>
            </div>
            <div>
              Public URL: <a href={justCreated.publicUrl} target="_blank" rel="noreferrer">{justCreated.publicUrl}</a>
            </div>
            <div>Username: {justCreated.adminUsername}</div>
            <div>Password: {justCreated.adminPassword}</div>
            <button className="btn btn-danger-ghost" onClick={() => setJustCreated(null)}>
              Dismiss
            </button>
          </div>
        )}

        <div className="slots">
          {slots.map((store, i) => {
            if (store) {
              return (
                <FilledSlot
                  key={store.storeId}
                  store={store}
                  onDelete={handleDeleteStore}
                  deleting={deletingStoreId === store.storeId}
                />
              );
            }
            if (i === stores.length && addingSlot) {
              return (
                <AddStoreForm
                  key="add-form"
                  tenantId={activeTenantId}
                  onSubmit={handleCreateStore}
                  onCancel={() => setAddingSlot(false)}
                  submitting={creatingStore}
                />
              );
            }
            if (i === stores.length && !atCapacity) {
              return (
                <EmptySlot key={`empty-${i}`} onAdd={() => setAddingSlot(true)} />
              );
            }
            return (
              <div key={`locked-${i}`} className="slot slot-empty" style={{ opacity: 0.3 }}>
                <div className="slot-empty-label">locked</div>
              </div>
            );
          })}
        </div>
      </main>
    </div>
  );
}
