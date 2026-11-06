// Starts the morning digest on time. GitHub's own schedule runs hours late in this repository.
// Separate from the live hoh Worker on purpose: deploying this one cannot affect broadcasts.
export default {
  async scheduled(_event, env) {
    const response = await fetch(`https://api.github.com/repos/${env.GITHUB_REPOSITORY}/dispatches`, {
      method: "POST",
      headers: {
        Accept: "application/vnd.github+json",
        Authorization: `Bearer ${env.GITHUB_DISPATCH_TOKEN}`,
        "Content-Type": "application/json",
        "User-Agent": "hoh-nhl-digest-cron",
        "X-GitHub-Api-Version": "2022-11-28",
      },
      body: JSON.stringify({ event_type: "nhl_morning_digest" }),
    });
    if (response.status !== 204) {
      throw new Error(`GitHub dispatch failed: HTTP ${response.status}`);
    }
  },
};
