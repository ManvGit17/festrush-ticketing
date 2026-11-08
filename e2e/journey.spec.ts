import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

const password = process.env.FESTRUSH_DEMO_PASSWORD ?? "festrush-demo";

test("sign in, watch the seat map, hold a seat, open the System Lens", async ({ page }) => {
  await page.goto("/login");
  await page.getByLabel("Username").fill("demo01");
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: /sign in/i }).click();
  await expect(page.getByText("Signed in as")).toBeVisible();

  await page.goto("/events");
  await page.getByRole("link", { name: "Live seat map" }).first().click();
  const map = page.getByRole("list", { name: "Seat map" });
  await expect(map).toBeVisible();
  const before = await page.locator(".seat.free[role=listitem]").count();

  await page.getByRole("button", { name: "Hold a seat" }).click();
  await expect(page.locator(".seat.mine.held")).toHaveCount(1);
  await expect(page.locator("#msg")).toContainText(/held|already/i);
  expect(await page.locator(".seat.free[role=listitem]").count()).toBeLessThanOrEqual(before);

  await page.getByRole("button", { name: "System Lens" }).click();
  await expect(page.locator("#lens dt", { hasText: "queue waiting" })).toBeVisible();
});

for (const path of ["/events", "/login", "/events/1/live"]) {
  test(`no serious accessibility violations on ${path}`, async ({ page }) => {
    await page.goto(path);
    const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze();
    const bad = results.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
    expect(bad.map((v) => v.id)).toEqual([]);
  });
}
