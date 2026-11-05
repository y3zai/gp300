import { buildHistoryPayloadQuery } from "./history.mjs";

const cors = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
};
const jsonHeaders = { ...cors, "Content-Type": "application/json" };
const dispatchUrl = "https://api.github.com/repos/y3zai/gp300/actions/workflows/update-index.yml/dispatches";

const currentQuery = `
  SELECT json_object(
    'index_value', index_value, 'timestamp', timestamp,
    'num_constituents', num_constituents, 'weighted_entropy', weighted_entropy,
    'divisor', divisor,
    'last_rebase', (SELECT CASE WHEN json_valid(data)
                               THEN json_extract(data, '$.last_rebase') END
                    FROM state WHERE id = 1)
  ) AS payload
  FROM current WHERE id = 1
`;

const constituentsQuery = `
  SELECT json_group_array(json_object(
    'id', id, 'label', label, 'source_type', source_type,
    'num_outcomes', num_outcomes, 'probabilities', json(probabilities),
    'normalized_entropy', normalized_entropy, 'weight', weight,
    'volume_1mo', volume_1mo, 'end_date', end_date, 'rank', rank
  ) ORDER BY weight DESC) AS payload
  FROM constituents
`;

function jsonText(body, status = 200) {
  return new Response(body, { status, headers: jsonHeaders });
}

async function historyResponse(db, since) {
  let query, parameters;
  try {
    [query, parameters] = buildHistoryPayloadQuery(since);
  } catch (error) {
    return jsonText(JSON.stringify({ error: error.message }), 400);
  }
  const { results } = await db.prepare(query).bind(...parameters).all();
  return jsonText("[" + results.map(({ payload }) => payload.slice(1, -1)).join(",") + "]");
}

export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") return new Response("", { headers: cors });

    const url = new URL(request.url);
    switch (url.pathname.replace(/\/+$/, "")) {
      case "/api/current": {
        const payload = await env.DB.prepare(currentQuery).first("payload");
        return payload ? jsonText(payload) : jsonText('{"error":"no data"}', 404);
      }
      case "/api/constituents":
        return jsonText(await env.DB.prepare(constituentsQuery).first("payload"));
      case "/api/history":
        return historyResponse(env.DB, url.searchParams.getAll("since").find(Boolean) ?? null);
      default:
        return jsonText('{"error":"not found"}', 404);
    }
  },

  async scheduled(controller, env) {
    const token = env.GITHUB_DISPATCH_TOKEN;
    if (!token) throw new Error("Missing GITHUB_DISPATCH_TOKEN");

    const slot = new Date(controller.scheduledTime);
    switch (controller.cron) {
      case "*/5 * * * *":
        slot.setUTCMinutes(Math.floor(slot.getUTCMinutes() / 5) * 5, 0, 0);
        break;
      case "1 0 * * MON-SAT":
      case "1 0 * * SUN":
        slot.setUTCHours(0, 1, 0, 0);
        break;
      default:
        throw new Error(`Unexpected Cron schedule: ${controller.cron}`);
    }

    const scheduledAt = slot.toISOString();
    const response = await fetch(dispatchUrl, {
      method: "POST",
      redirect: "error",
      signal: AbortSignal.timeout(15_000),
      headers: {
        "Accept": "application/vnd.github+json",
        "Authorization": `Bearer ${token}`,
        "Content-Type": "application/json",
        "User-Agent": "gp300-scheduler",
        "X-GitHub-Api-Version": "2026-03-10",
      },
      body: JSON.stringify({
        ref: "main",
        inputs: { scheduled_at: scheduledAt, dry_run: false },
      }),
    });
    if (!response.ok) {
      await response.body?.cancel();
      throw new Error(`GitHub dispatch returned HTTP ${response.status} for ${scheduledAt}`);
    }
    const runId = response.status === 200 ? (await response.json()).workflow_run_id : null;
    console.log("Dispatched GP300 update", { scheduledAt, runId });
  },
};
