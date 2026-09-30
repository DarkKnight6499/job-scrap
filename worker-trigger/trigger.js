// Starts the job-scout workflow on schedule; GitHub's own cron is too slow and stays as backup.
const DISPATCH = "https://api.github.com/repos/DarkKnight6499/job-scrap/actions/workflows/scout.yml/dispatches";

async function ntfy(env, message) {
  if (!env.NTFY_TOPIC) return;
  await fetch(`https://ntfy.sh/${env.NTFY_TOPIC}`, { method: "POST", body: message, headers: { Title: "job-scout trigger failed" } });
}

async function dispatch(env) {
  const res = await fetch(DISPATCH, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GH_TOKEN}`,
      Accept: "application/vnd.github+json",
      "User-Agent": "job-scout-trigger",
      "X-GitHub-Api-Version": "2022-11-28",
    },
    body: JSON.stringify({ ref: "main" }),
  });
  if (res.status !== 204) {
    const detail = `${res.status} ${(await res.text()).slice(0, 200)}`;
    await ntfy(env, `workflow_dispatch returned ${detail} (token expired or revoked?)`);
    throw new Error(detail);
  }
}

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(dispatch(env));
  },
};
