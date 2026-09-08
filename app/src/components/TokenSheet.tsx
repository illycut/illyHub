/** Visual QA page for every design token (ai-dev #27). Static; reads the CSS variables. */
const COLORS = [
  "--bg-base", "--bg-raised", "--bg-overlay", "--stroke", "--text-primary", "--text-secondary", "--text-tertiary",
  "--signal", "--signal-dim", "--sync-locked", "--sync-drift", "--sync-lost", "--error",
  "--badge-tidal-bg", "--badge-tidal-fg", "--badge-ytmusic-bg", "--badge-ytmusic-fg", "--badge-pandora-bg", "--badge-pandora-fg",
];
const TYPE = ["display", "title-1", "title-2", "body", "caption", "micro"] as const;
const SPACE = ["--space-1", "--space-2", "--space-3", "--space-4", "--space-6", "--space-8", "--space-12"];
const RADII = ["--radius-art", "--radius-control", "--radius-sheet", "--radius-pill"];

export function TokenSheet() {
  return (
    <main className="screen-margin flex flex-col gap-8 py-8">
      <h1 className="text-title-1">Design tokens</h1>
      <section>
        <h2 className="text-title-2">Color</h2>
        <ul className="mt-3 grid grid-cols-2 gap-3 tablet:grid-cols-4">
          {COLORS.map((c) => (
            <li key={c} className="flex items-center gap-3">
              <span className="h-10 w-10 rounded-control border border-stroke" style={{ background: `var(${c})` }} />
              <code className="text-caption text-secondary">{c}</code>
            </li>
          ))}
        </ul>
      </section>
      <section>
        <h2 className="text-title-2">Type</h2>
        <ul className="mt-3 flex flex-col gap-2">
          {TYPE.map((t) => (
            <li key={t} className={`text-${t}`}>
              {t} — The quick amber fox 0123456789
            </li>
          ))}
          <li className="text-body numeric">numeric 12:34 · 56:78 · 100</li>
        </ul>
      </section>
      <section>
        <h2 className="text-title-2">Spacing</h2>
        <ul className="mt-3 flex items-end gap-3">
          {SPACE.map((s) => (
            <li key={s} className="flex flex-col items-center gap-1">
              <span className="bg-signal" style={{ width: `var(${s})`, height: `var(${s})` }} />
              <code className="text-micro text-tertiary">{s.replace("--space-", "")}</code>
            </li>
          ))}
        </ul>
      </section>
      <section>
        <h2 className="text-title-2">Radii</h2>
        <ul className="mt-3 flex gap-4">
          {RADII.map((r) => (
            <li key={r} className="flex flex-col items-center gap-1">
              <span className="h-12 w-12 bg-overlay" style={{ borderRadius: `var(${r})` }} />
              <code className="text-micro text-tertiary">{r.replace("--radius-", "")}</code>
            </li>
          ))}
        </ul>
      </section>
    </main>
  );
}
