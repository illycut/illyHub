import { readFileSync, readdirSync, statSync } from "node:fs";
import path from "node:path";
import postcss from "postcss";
import tailwindcss from "tailwindcss";
import config from "../../tailwind.config";

/**
 * Builds the real Tailwind output for the source tree and fails if any class used in a
 * `className` string produced no CSS rule. This is what caught h-7 / w-10 / bg-base/60 emitting
 * nothing after the theme was replaced (UX U2/U3).
 */
function walk(dir: string, out: string[] = []): string[] {
  for (const f of readdirSync(dir)) {
    const p = path.join(dir, f);
    if (statSync(p).isDirectory()) walk(p, out);
    else if (/\.tsx?$/.test(f) && !/\.test\.tsx?$/.test(f) && !f.endsWith(".d.ts")) out.push(p);
  }
  return out;
}

const SRC = path.resolve(__dirname, "..");
const files = walk(SRC);

// Class names declared in globals.css (custom utilities/components), plus Tailwind's own sr-only.
const globals = readFileSync(path.join(__dirname, "globals.css"), "utf8");
const custom = new Set([...globals.matchAll(/^\s*\.([a-zA-Z0-9_-]+)/gm)].map((m) => m[1]!));

function classesIn(source: string): string[] {
  const out: string[] = [];
  // className="..." and className={`...`} and plain "..." fragments inside className expressions
  const attr = /className=\{?["'`]([^"'`]*)["'`]/g;
  for (const m of source.matchAll(attr)) out.push(...m[1]!.split(/\s+/));
  const tpl = /className=\{`([\s\S]*?)`\}/g;
  for (const m of source.matchAll(tpl)) {
    out.push(...m[1]!.replace(/\$\{[\s\S]*?\}/g, " ").split(/\s+/));
    // ternary branches inside this template's ${ cond ? "a b" : "c" } expressions
    for (const e of m[1]!.matchAll(/\$\{([^}]*)\}/g)) for (const b of e[1]!.matchAll(/[?:]\s*["']([^"']*)["']/g)) out.push(...b[1]!.split(/\s+/));
  }
  // Tailwind-shaped tokens only: optional variants, a lowercase utility, optional [arbitrary] and /modifier.
  const shape = /^(?:[a-z0-9-]+:)*-?[a-z][a-z0-9-]*(?:\[[^\s\]]+\])?(?:\/(?:\d+|\[[^\]]+\]))?$/;
  const literals = new Set(["null", "undefined", "true", "false"]);
  return out.filter((c) => c && !c.includes("${") && !literals.has(c) && shape.test(c));
}

/** Unescape a CSS selector back to the class string Tailwind was given. */
function unescapeSelector(sel: string): string {
  return sel.replace(/\\([0-9a-f]{1,6}) ?/gi, (_, hex: string) => String.fromCodePoint(parseInt(hex, 16))).replace(/\\(.)/g, "$1");
}

describe("tailwind classes used in components produce CSS", () => {
  it("every class used in className produces a rule", async () => {
    const used = new Set<string>();
    for (const f of files) for (const c of classesIn(readFileSync(f, "utf8"))) used.add(c);
    const result = await postcss([tailwindcss({ ...config, content: files })]).process("@tailwind utilities; @tailwind components;", { from: undefined });
    const selectors: string[] = [];
    result.root.walkRules((r) => {
      selectors.push(unescapeSelector(r.selector));
    });
    const haystack = selectors.join("\n");
    const missing = [...used].filter((c) => {
      if (custom.has(c)) return false;
      if (c === "sr-only") return false;
      const bare = c.replace(/^(tablet|np-landscape|hover|focus|active|disabled|first|last):/, "");
      if (custom.has(bare)) return false;
      return !haystack.includes(`.${c}`);
    });
    expect(missing, `classes with no CSS: ${missing.join(", ")}`).toEqual([]);
    expect(used.size).toBeGreaterThan(50);
  });

  it("alpha modifiers on token colors emit rgb() with alpha", async () => {
    const result = await postcss([tailwindcss({ ...config, content: [{ raw: '<div class="bg-base/60 via-base/40 bg-signal/20"></div>' }] })]).process("@tailwind utilities;", { from: undefined });
    expect(result.css).toMatch(/\.bg-base\\\/60\s*{[^}]*rgb\(var\(--bg-base-rgb\) \/ 0\.6\)/);
    expect(result.css).toMatch(/\.bg-signal\\\/20\s*{[^}]*rgb\(var\(--signal-rgb\) \/ 0\.2\)/);
    expect(result.css).toContain("via-base");
  });
});
