// Browser regression: run against a local static server or a deployed dashboard:
// playwright-cli open http://127.0.0.1:8000
// playwright-cli run-code --filename tests/chart-ranges.js
// Uses the real chart library with deterministic API fixtures and a fixed clock.
async (page) => {
  const now = Date.parse('2026-09-29T04:00:00Z');
  const start = Date.parse('2026-03-04T06:00:00Z');
  const recent = now - 7 * 86400000;
  const history = [];
  for (let time = start; time <= now; time += time < recent ? 3600000 : 300000) {
    history.push({ timestamp: new Date(time).toISOString(), value: 500,
      num_constituents: 1, weighted_entropy: 0.5, divisor: 0.001 });
  }
  const current = { index_value: 500, timestamp: new Date(now).toISOString(),
    num_constituents: 1, weighted_entropy: 0.5, divisor: 0.001,
    last_rebase: '2026-08-29T00:00:00Z' };
  const constituents = [{ id: 'test', label: 'Test contract', source_type: 'market',
    num_outcomes: 2, probabilities: [0.5, 0.5], normalized_entropy: 1,
    weight: 1, volume_1mo: 100, end_date: null, rank: 1 }];
  const errors = [];
  const failures = [];
  const onError = error => errors.push(error.message);
  page.on('pageerror', onError);
  const apiRoute = async route => {
    const endpoint = new URL(route.request().url()).pathname;
    const body = { '/api/history': history, '/api/current': current,
      '/api/constituents': constituents }[endpoint];
    if (body === undefined) return route.continue();
    return route.fulfill({ json: body });
  };
  await page.clock.setFixedTime(new Date(now));
  await page.route('**/api/**', apiRoute);
  try {
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.reload();
    await page.waitForFunction(() => typeof areaSeries !== 'undefined' &&
      areaSeries !== null && areaSeries.data().length > 0);
    const check = async label => {
      // Wait for resize observer, chart layout, and the scheduled fitContent.
      await page.evaluate(() => new Promise(resolve =>
        requestAnimationFrame(() => requestAnimationFrame(resolve))));
      const state = await page.evaluate(() => {
        const points = areaSeries.data();
        return { count: points.length, first: points[0].time,
          last: points[points.length - 1].time,
          visible: chart.timeScale().getVisibleRange() };
      });
      if (state.count < 2000 || !state.visible ||
          state.visible.from > state.first || state.visible.to < state.last) {
        failures.push(`${label}: selected range is clipped: ${JSON.stringify(state)}`);
      }
      console.log(label, JSON.stringify(state));
    };
    for (const range of ['3M', 'YTD', 'MAX']) {
      await page.getByRole('button', { name: range, exact: true }).click();
      await check(`desktop ${range}`);
    }
    await page.setViewportSize({ width: 390, height: 844 });
    await check('MAX after shrinking to mobile');
    for (const range of ['3M', 'YTD']) {
      await page.getByRole('button', { name: range, exact: true }).click();
      await check(`mobile ${range}`);
    }
    await page.setViewportSize({ width: 1280, height: 900 });
    await check('YTD after expanding to desktop');
    if (errors.length) failures.push(`Browser errors: ${errors.join('; ')}`);
    if (failures.length) throw new Error(failures.join('\n'));
  } finally {
    page.off('pageerror', onError);
    await page.unroute('**/api/**', apiRoute);
  }
}
