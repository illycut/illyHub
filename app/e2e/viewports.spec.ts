import { devices, expect, test } from "@playwright/test";

/**
 * Now Playing must never require scrolling to reach play/pause (UX U5): the transport row sits
 * inside the viewport on a short phone, a phone in landscape, and a landscape tablet.
 */
const cases = [
  { name: "iPhone SE portrait", viewport: { width: 375, height: 667 } },
  { name: "iPhone 13 landscape", viewport: { width: 844, height: 390 } },
  { name: "iPad landscape", viewport: { width: 1080, height: 810 } },
  { name: "Pixel 7 portrait", viewport: devices["Pixel 7"].viewport },
];

for (const c of cases) {
  test(`transport row is within the viewport on ${c.name}`, async ({ page }) => {
    await page.setViewportSize(c.viewport);
    await page.goto("/");
    await page.getByRole("button", { name: "Open Now Playing" }).click();
    await expect(page.getByTestId("now-playing")).toBeVisible();
    const toggle = page.getByTestId("play-toggle");
    await expect(toggle).toBeVisible();
    const box = await toggle.boundingBox();
    expect(box).not.toBeNull();
    expect(box!.y).toBeGreaterThanOrEqual(0);
    expect(box!.y + box!.height).toBeLessThanOrEqual(c.viewport.height + 1);
    // 56px targets with >= 8px between neighbours
    const buttons = page.getByTestId("transport").getByRole("button");
    const boxes = await buttons.evaluateAll((els) => els.map((e) => e.getBoundingClientRect()).map((r) => ({ x: r.x, w: r.width })));
    for (let i = 1; i < boxes.length; i++) expect(boxes[i]!.x - (boxes[i - 1]!.x + boxes[i - 1]!.w)).toBeGreaterThanOrEqual(8);
    const layer = page.getByTestId("now-playing-layer");
    const scroll = await layer.evaluate((el) => el.scrollHeight - el.clientHeight);
    expect(scroll).toBeLessThanOrEqual(1);
  });
}
