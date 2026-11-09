import { test, expect } from "@playwright/test";

test("simple topic page, connection handoff, and complete demo", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "What topic do you want to debate?" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Continue with ChatGPT" }),
  ).toBeVisible();
  await page
    .getByLabel("What topic do you want to debate?")
    .fill("Should cities make public transit free?");
  await expect(
    page.getByRole("button", { name: "Debate", exact: true }),
  ).toBeDisabled();
  await page.screenshot({
    path: "test-results/start-desktop.png",
    fullPage: true,
  });
  // Verify registration setup without leaving for or approving a real OAuth grant.
  const connection = await page.request.post("/api/auth/chatgpt/start", {
    data: {},
  });
  expect(connection.ok()).toBeTruthy();
  expect((await connection.json()).authorization_url).toContain(
    "client_id=dynamic_agent_client",
  );
  const demo = await page.request.post("/api/runs", {
    data: {
      question: "Offline full workflow check",
      model_config: { provider: "demo" },
    },
  });
  expect(demo.ok()).toBeTruthy();
  await page.goto(`/#${(await demo.json()).id}`);
  const verdictButton = page.getByRole("button", { name: /Verdict made/ });
  await expect(
    page.getByText("Final outcome", { exact: true }).or(verdictButton),
  ).toBeVisible();
  if (await verdictButton.isVisible()) await verdictButton.click();
  await expect(page.getByText("Final outcome", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "What’s being debated" }),
  ).toBeVisible();
  await expect(page.getByText("Fictional demo · no charges")).toBeVisible();
  await page.screenshot({
    path: "test-results/assessment-desktop.png",
    fullPage: true,
  });
  const reference = page.locator(".outcome .refs a").first();
  await reference.click();
  await expect(page.locator("details.evidence")).toHaveAttribute("open", "");
  const exported = await page.request.get(
    `/api/runs/${new URL(page.url()).hash.slice(1)}/report?format=html`,
  );
  expect(exported.ok()).toBeTruthy();
  expect(await exported.text()).toContain('href="#claim_');
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "test-results/assessment-mobile.png",
    fullPage: true,
  });
  expect(errors).toEqual([]);
});
