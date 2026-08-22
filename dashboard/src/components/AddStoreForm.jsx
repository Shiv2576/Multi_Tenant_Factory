import { useMemo, useState } from "react";
import { PasswordField } from "./PasswordField";

function randomSuffix() {
  return Math.random().toString(36).slice(2, 6);
}

function randomPassword() {
  const bytes = new Uint8Array(9);
  crypto.getRandomValues(bytes);
  return btoa(String.fromCharCode(...bytes)).replace(/[+/=]/g, "");
}

// nip.io resolves "anything.127.0.0.1.nip.io" to 127.0.0.1 over real public
// DNS — zero /etc/hosts editing, for any store, forever. A plain ".local"
// domain looks nicer but silently fails to resolve (DNS_PROBE_POSSIBLE) until
// someone manually adds it to /etc/hosts on every machine that visits it.
function buildDomain(storeId, tenantId) {
  return `${storeId}.${tenantId}.127.0.0.1.nip.io`;
}

export function AddStoreForm({ onSubmit, onCancel, tenantId, submitting }) {
  const defaultStoreId = useMemo(() => `shop-${randomSuffix()}`, []);
  const defaultPassword = useMemo(() => randomPassword(), []);

  const [storeId, setStoreId] = useState(defaultStoreId);
  const [domain, setDomain] = useState(buildDomain(defaultStoreId, tenantId));
  const [domainTouched, setDomainTouched] = useState(false);
  const [plan, setPlan] = useState("basic");
  const [adminUsername, setAdminUsername] = useState("admin");
  const [adminEmail, setAdminEmail] = useState(
    `admin@${buildDomain(defaultStoreId, tenantId)}`
  );
  const [adminEmailTouched, setAdminEmailTouched] = useState(false);
  const [adminPassword, setAdminPassword] = useState(defaultPassword);

  function handleStoreIdChange(value) {
    const nextStoreId = value.toLowerCase();
    setStoreId(nextStoreId);
    if (!domainTouched) setDomain(buildDomain(nextStoreId, tenantId));
    if (!adminEmailTouched)
      setAdminEmail(`admin@${buildDomain(nextStoreId, tenantId)}`);
  }

  function handleSubmit(e) {
    e.preventDefault();
    onSubmit({
      storeId: storeId.trim(),
      domain: domain.trim(),
      plan,
      adminUsername: adminUsername.trim(),
      adminEmail: adminEmail.trim(),
      adminPassword: adminPassword.trim(),
    });
  }

  return (
    <form className="slot slot-filled" data-phase="Pending" onSubmit={handleSubmit}>
      <div className="field">
        <label className="field-label">Store ID</label>
        <input
          value={storeId}
          onChange={(e) => handleStoreIdChange(e.target.value)}
          pattern="[a-z0-9-]+"
          required
          autoFocus
        />
      </div>
      <div className="field">
        <label className="field-label">Domain</label>
        <input
          value={domain}
          onChange={(e) => {
            setDomainTouched(true);
            setDomain(e.target.value);
          }}
        />
      </div>
      <div className="field">
        <label className="field-label">Plan</label>
        <select value={plan} onChange={(e) => setPlan(e.target.value)}>
          <option value="basic">basic</option>
          <option value="pro">pro</option>
        </select>
      </div>
      <div className="field">
        <label className="field-label">Admin username</label>
        <input
          value={adminUsername}
          onChange={(e) => setAdminUsername(e.target.value)}
        />
      </div>
      <div className="field">
        <label className="field-label">Admin email</label>
        <input
          value={adminEmail}
          onChange={(e) => {
            setAdminEmailTouched(true);
            setAdminEmail(e.target.value);
          }}
        />
      </div>
      <div className="field">
        <label className="field-label">Admin password</label>
        <PasswordField
          value={adminPassword}
          onChange={(e) => setAdminPassword(e.target.value)}
        />
      </div>
      <div className="slot-actions">
        <button className="btn btn-primary" type="submit" disabled={submitting}>
          {submitting ? "Creating…" : "Create"}
        </button>
        <button
          className="btn btn-danger-ghost"
          type="button"
          onClick={onCancel}
          disabled={submitting}
        >
          Cancel
        </button>
      </div>
    </form>
  );
}
