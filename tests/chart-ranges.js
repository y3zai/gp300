// Browser regression callback. Pass a Playwright page open on the archive.
// Uses the real chart library with a deterministic archive and a later wall clock.
async (page) => {
  const final = Date.parse('2026-09-29T04:00:00Z');
  const start = Date.parse('2026-03-04T06:00:00Z');
  const history = [];
  for (let time = start; time <= final; time += 300000) {
    history.push({ timestamp: new Date(time).toISOString(), value: 500,
      num_constituents: 1, weighted_entropy: 0.5, divisor: 0.001 });
  }
  const errors = [];
  const failures = [];
  const onError = error => errors.push(error.message);
  page.on('pageerror', onError);
  const requests = [];
  const onRequest = request => requests.push(new URL(request.url()).pathname);
  page.on('request', onRequest);
  let archiveRequests = 0;
  const historyRoute = route => {
    archiveRequests++;
    return route.fulfill({ json: history });
  };
  await page.clock.setFixedTime(new Date('2029-04-20T12:00:00Z'));
  await page.route('**/history.json', historyRoute);
  try {
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.reload();
    await page.waitForFunction(() => typeof areaSeries !== 'undefined' &&
      areaSeries !== null && areaSeries.data().length > 0);
    const check = async (label, first, minCount) => {
      // Wait for resize observer, chart layout, and the scheduled fitContent.
      await page.evaluate(() => new Promise(resolve =>
        requestAnimationFrame(() => requestAnimationFrame(resolve))));
      const state = await page.evaluate(() => {
        const points = areaSeries.data();
        return { count: points.length, first: points[0].time,
          last: points[points.length - 1].time,
          visible: chart.timeScale().getVisibleRange() };
      });
      if (state.count < minCount || state.first !== first || state.last !== final / 1000 || !state.visible ||
          state.visible.from > state.first || state.visible.to < state.last) {
        failures.push(`${label}: selected range is clipped: ${JSON.stringify(state)}`);
      }
      console.log(label, JSON.stringify(state));
    };
    const cutoff = {
      '1D': (final - 86400000) / 1000,
      '3M': Date.parse('2026-06-29T00:00:00Z') / 1000,
      YTD: start / 1000,
      MAX: start / 1000,
    };
    if (!(await page.getByText('Final observation', { exact: false }).count())) {
      failures.push('Archive is missing a final observation label');
    }
    for (const range of ['1D', '3M', 'YTD', 'MAX']) {
      await page.getByRole('button', { name: range, exact: true }).click();
      await check(`desktop ${range}`, cutoff[range], range === '1D' ? 288 : 2000);
    }
    await page.setViewportSize({ width: 390, height: 844 });
    await check('MAX after shrinking to mobile', cutoff.MAX, 2000);
    for (const range of ['3M', 'YTD']) {
      await page.getByRole('button', { name: range, exact: true }).click();
      await check(`mobile ${range}`, cutoff[range], 2000);
    }
    await page.setViewportSize({ width: 1280, height: 900 });
    await check('YTD after expanding to desktop', cutoff.YTD, 2000);
    if (archiveRequests !== 1 || requests.some(path => path.startsWith('/api/'))) {
      failures.push(`Expected one static history request and no API requests. Observed: ${requests.join(', ')}`);
    }
    if (errors.length) failures.push(`Browser errors: ${errors.join('; ')}`);
    if (failures.length) throw new Error(failures.join('\n'));
  } finally {
    page.off('pageerror', onError);
    page.off('request', onRequest);
    await page.unroute('**/history.json', historyRoute);
  }
}
