import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";

const config = [
  ...nextVitals,
  ...nextTs,
  {
    ignores: ["android/**", "out/**", ".next/**", "node_modules/**", "coverage/**", "playwright-report/**", "test-results/**", "src/lib/hub/openapi.d.ts"],
  },
];

export default config;
