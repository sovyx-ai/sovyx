/**
 * NeuralMesh — Subtle animated background for the Overview page.
 *
 * CSS-only radial gradient dots with a gentle pulse animation.
 * No canvas, no JS animation loops — pure CSS for <1% CPU impact.
 * Respects prefers-reduced-motion (animation disabled globally).
 *
 * Ref: META-01 §15 (Neural Mesh spec)
 */

export function NeuralMesh() {
  return (
    <div
      className="pointer-events-none absolute inset-0 overflow-hidden"
      aria-hidden="true"
    >
      {/* Dot grid pattern */}
      <div
        className="absolute inset-0 animate-[pulse-dot_4s_ease-in-out_infinite]"
        style={{
          backgroundImage:
            "radial-gradient(circle, var(--svx-color-brand-primary) 1px, transparent 1px)",
          backgroundSize: "48px 48px",
          opacity: 0.04,
        }}
      />
      {/* Subtle brand glow — top right */}
      <div
        className="absolute -right-32 -top-32 size-96 rounded-full"
        style={{
          background:
            "radial-gradient(circle, rgba(139,92,246,0.06) 0%, transparent 70%)",
        }}
      />
    </div>
  );
}
