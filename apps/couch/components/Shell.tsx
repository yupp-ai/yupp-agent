import { Rail } from "./Rail";
import { RailResizer } from "./RailResizer";

export async function Shell({
  active,
  activeSessionId,
  hasRight,
  rightCollapsed,
  children,
  right,
  resizer,
}: {
  active?: string;
  activeSessionId?: string;
  hasRight?: boolean;
  rightCollapsed?: boolean;
  children: React.ReactNode;
  right?: React.ReactNode;
  resizer?: React.ReactNode;
}) {
  const cls = `shell${hasRight ? " has-right" : ""}${rightCollapsed ? " right-collapsed" : ""}`;
  return (
    <div className={cls}>
      <Rail active={active} activeSessionId={activeSessionId} />
      <RailResizer />
      {children}
      {hasRight && (
        <>
          {resizer}
          {right}
        </>
      )}
    </div>
  );
}
