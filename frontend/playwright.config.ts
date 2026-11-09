import { defineConfig, devices } from "@playwright/test";
export default defineConfig({
  testDir: "./e2e",
  timeout: 45000,
  expect: { timeout: 20000 },
  fullyParallel: false,
  workers: 1,
  use: {
    baseURL: "http://127.0.0.1:8001",
    ...devices["Desktop Chrome"],
    channel: "chrome",
    trace: "retain-on-failure",
  },
  webServer: {
    command: "../.venv/bin/debate-bro serve --port 8001",
    url: "http://127.0.0.1:8001/api/health",
    reuseExistingServer: false,
    env: {
      DEBATE_DATA_DIR: "/private/tmp/debate-bro-playwright",
      OPENAI_API_KEY: "",
    },
  },
});
