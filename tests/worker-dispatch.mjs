import assert from "node:assert/strict";
import test from "node:test";
import worker from "../src/worker.mjs";

test("dispatches the five-minute and midnight slots to the production workflow", async (t) => {
  const requests = [];
  t.mock.method(globalThis, "fetch", async (url, options) => {
    requests.push([url, options]);
    return Response.json({ workflow_run_id: 12345 });
  });
  const env = { GITHUB_DISPATCH_TOKEN: "test-token" };

  for (const [cron, time, expected] of [
    ["*/5 * * * *", "2026-11-04T16:25:05Z", "2026-11-04T16:25:00.000Z"],
    ["1 0 * * MON-SAT", "2026-11-04T00:01:05Z", "2026-11-04T00:01:00.000Z"],
    ["1 0 * * SUN", "2026-11-08T00:01:05Z", "2026-11-08T00:01:00.000Z"],
  ]) {
    await worker.scheduled({ cron, scheduledTime: Date.parse(time) }, env);
    const [url, options] = requests.at(-1);
    assert.match(url, /\/repos\/y3zai\/gp300\/actions\/workflows\/update-index\.yml\/dispatches$/);
    assert.equal(options.method, "POST");
    assert.equal(options.redirect, "manual");
    assert.equal(options.headers.Authorization, "Bearer test-token");
    assert.deepEqual(JSON.parse(options.body), {
      ref: "main",
      inputs: { scheduled_at: expected, dry_run: false },
    });
  }
});

test("reports a rejected dispatch without including its response body", async (t) => {
  t.mock.method(globalThis, "fetch", async () => new Response("private details", { status: 403 }));
  await assert.rejects(
    worker.scheduled({
      cron: "*/5 * * * *",
      scheduledTime: Date.parse("2026-11-04T16:25:00Z"),
    }, { GITHUB_DISPATCH_TOKEN: "test-token" }),
    { message: "GitHub dispatch returned HTTP 403 for 2026-11-04T16:25:00.000Z" },
  );
});
