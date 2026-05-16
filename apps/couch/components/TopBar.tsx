import Link from "next/link";

export interface CrumbStep {
  href?: string;
  label: string;
}

export function TopBar({
  back,
  crumbs,
  meta,
  actions,
}: {
  back?: { href: string; label: string };
  crumbs: CrumbStep[];
  meta?: React.ReactNode;
  actions?: React.ReactNode;
}) {
  // If the caller supplies both `back` and a first breadcrumb that targets
  // the same URL, the words read twice ("← Sessions   Sessions / …"). Render
  // the back arrow as part of the first crumb in that case, so the result is
  // "← Sessions / <leaf>".
  const firstCrumb = crumbs[0];
  const mergeBack =
    back &&
    firstCrumb &&
    firstCrumb.href === back.href &&
    firstCrumb.label.toLowerCase() === back.label.toLowerCase();

  return (
    <div className="top">
      {back && !mergeBack && (
        <Link href={back.href} className="back-link">
          ← {back.label}
        </Link>
      )}
      <div className="crumb">
        {crumbs.map((c, i) => {
          const isLast = i === crumbs.length - 1;
          const prefix = i === 0 && mergeBack ? "← " : "";
          return (
            <span key={i} style={{ display: "inline-flex", gap: 8 }}>
              {i > 0 && <span className="sep">/</span>}
              {c.href && !isLast ? (
                <Link href={c.href}>{prefix}{c.label}</Link>
              ) : isLast ? (
                <b>{prefix}{c.label}</b>
              ) : (
                <span>{prefix}{c.label}</span>
              )}
            </span>
          );
        })}
      </div>
      {meta && (
        <span className="hint-xs">{meta}</span>
      )}
      <span className="grow"></span>
      {actions}
    </div>
  );
}
