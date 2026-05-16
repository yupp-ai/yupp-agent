import { triggerClass, triggerLetter } from "@/lib/format";

type Variant = "letter" | "wide";

export function Tpill({ trigger, variant = "letter" }: { trigger: string; variant?: Variant }) {
  const upper = (trigger || "").toUpperCase();
  // WEB is the default — no leading pill in the past-sessions list.
  if (variant === "letter" && upper === "WEB") return null;
  const letter = triggerLetter(upper);
  if (variant === "wide") {
    return (
      <span className={`tpill wide ${triggerClass(upper)}`} title={upper.toLowerCase()}>
        {upper.toLowerCase()}
      </span>
    );
  }
  return (
    <span className={`tpill ${triggerClass(upper)}`} title={upper.toLowerCase()}>
      {letter ?? ""}
    </span>
  );
}
