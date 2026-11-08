import { defineConfig } from "@playwright/test";

// Expects a running, seeded app (see .github/workflows/ci.yml). E2E_BASE_URL overrides the address.
export default defineConfig({
  testDir: ".",
  testMatch: "*.spec.ts",
  workers: 1,
  use: { baseURL: process.env.E2E_BASE_URL ?? "http://localhost:8080" },
});
