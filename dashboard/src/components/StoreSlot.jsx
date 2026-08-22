export function EmptySlot({ onAdd, disabled }) {
  return (
    <button
      className="slot slot-empty"
      onClick={onAdd}
      disabled={disabled}
      type="button"
    >
      <div className="slot-empty-plus">+</div>
      <div className="slot-empty-label">Add store</div>
    </button>
  );
}

export function FilledSlot({ store, onDelete, deleting }) {
  const phase = deleting ? "Deleting" : store.phase || "Pending";
  const label = deleting ? "Tearing down" : phase;

  return (
    <div className="slot slot-filled" data-phase={phase}>
      <div className="slot-top">
        <div className="slot-storeid">{store.storeId}</div>
        <div className="plan-badge" data-plan={store.plan}>
          {store.plan}
        </div>
      </div>
      <div className="slot-domain">{store.domain}</div>
      {(store.publicUrl || store.adminUrl) && (
        <div className="slot-links">
          {store.publicUrl && (
            <a
              className="slot-link slot-link-public"
              href={store.publicUrl}
              target="_blank"
              rel="noreferrer"
            >
              <span className="slot-link-dot" />
              Public site
              <span className="slot-link-arrow">↗</span>
            </a>
          )}
          {store.adminUrl && (
            <a
              className="slot-link slot-link-admin"
              href={store.adminUrl}
              target="_blank"
              rel="noreferrer"
            >
              <span className="slot-link-dot" />
              Admin
              <span className="slot-link-arrow">↗</span>
            </a>
          )}
        </div>
      )}
      <div className="slot-status-row">
        <span className="status-dot" data-phase={phase} />
        <span className="status-label">{label}</span>
      </div>
      <div className="slot-actions">
        <button
          className="btn btn-danger-ghost"
          onClick={() => onDelete(store.storeId)}
          disabled={deleting}
          type="button"
        >
          {deleting ? "Deleting…" : "Delete store"}
        </button>
      </div>
    </div>
  );
}
