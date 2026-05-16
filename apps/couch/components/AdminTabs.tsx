"use client";

import { useState } from "react";

export function AdminTabs<T extends string>({
  tabs,
  initial,
  renderPanel,
}: {
  tabs: { key: T; label: string }[];
  initial: T;
  renderPanel: (active: T) => React.ReactNode;
}) {
  const [active, setActive] = useState<T>(initial);
  return (
    <>
      <div className="admin-tabs">
        {tabs.map((t) => (
          <button
            key={t.key}
            type="button"
            className={`admin-tab${t.key === active ? " on" : ""}`}
            onClick={() => setActive(t.key)}
          >
            {t.label}
          </button>
        ))}
      </div>
      <div className="admin-tab-panel">{renderPanel(active)}</div>
    </>
  );
}
