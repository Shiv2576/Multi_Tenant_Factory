import { useState } from "react";
import { createApi } from "../api";
import { PasswordField } from "./PasswordField";

export function AuthPage({ apiBase, setApiBase, onAuthenticated }) {
  const [authTenantId, setAuthTenantId] = useState("");
  const [authPassword, setAuthPassword] = useState("");
  const [authMode, setAuthMode] = useState("login"); // "login" | "register"
  const [authenticating, setAuthenticating] = useState(false);
  const [error, setError] = useState("");

  async function handleSubmit(e) {
    e.preventDefault();
    const id = authTenantId.trim().toLowerCase();
    const password = authPassword;
    if (!id || !password) return;
    setAuthenticating(true);
    setError("");
    try {
      const api = createApi(apiBase, null);
      const result =
        authMode === "register"
          ? await api.register(id, password)
          : await api.login(id, password);
      onAuthenticated(result.token, result.tenantId);
    } catch (e) {
      setError(e.message);
    } finally {
      setAuthenticating(false);
    }
  }

  return (
    <div className="auth-shell">
      <form className="auth-card" onSubmit={handleSubmit}>
        <div className="brand">
          <span className="brand-mark" />
          <span className="brand-title">STORE FACTORY</span>
        </div>
        <p className="brand-sub">
          {authMode === "register"
            ? "register a new tenant to begin"
            : "log in to your tenant"}
        </p>

        <div className="field">
          <label className="field-label">Control Plane API</label>
          <input
            value={apiBase}
            onChange={(e) => setApiBase(e.target.value)}
            placeholder="http://localhost:8000"
          />
        </div>

        <hr className="divider" />

        <div className="field">
          <label className="field-label">Tenant ID</label>
          <input
            value={authTenantId}
            onChange={(e) => setAuthTenantId(e.target.value.toLowerCase())}
            placeholder="tenant001"
            pattern="[a-z0-9-]+"
            required
            autoFocus
          />
        </div>
        <div className="field">
          <label className="field-label">Password</label>
          <PasswordField
            value={authPassword}
            onChange={(e) => setAuthPassword(e.target.value)}
            placeholder="••••••••"
            minLength={8}
            required
          />
        </div>

        {error && <div className="error-banner">{error}</div>}

        <button className="btn btn-primary" type="submit" disabled={authenticating}>
          {authenticating
            ? authMode === "register"
              ? "Registering…"
              : "Logging in…"
            : authMode === "register"
              ? "Register new tenant"
              : "Log in"}
        </button>
        <button
          className="btn btn-danger-ghost"
          type="button"
          onClick={() =>
            setAuthMode((m) => (m === "register" ? "login" : "register"))
          }
          disabled={authenticating}
        >
          {authMode === "register"
            ? "Already have a tenant? Log in"
            : "New here? Register a tenant"}
        </button>
      </form>
    </div>
  );
}
