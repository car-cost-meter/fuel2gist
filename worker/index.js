/**
 * Cloudflare Worker — hourly trigger for the fuel2gist GitHub Actions workflow.
 *
 * Authenticates as a GitHub App installation (RS256 JWT -> installation
 * token) and calls the workflow_dispatch endpoint. No personal token:
 * the App is org-owned and its private key never expires.
 *
 * Secrets (wrangler secret put / dashboard Variables and Secrets):
 *   GITHUB_APP_ID          App id (from the App settings page)
 *   GITHUB_INSTALLATION_ID Installation id (URL of the installed app)
 *   GITHUB_APP_KEY         Private key, PEM format (multiline)
 */

const REPO = "car-cost-meter/fuel2gist";
const API = "https://api.github.com";

// Cron expression -> workflow file. The worker is triggered by several
// cron triggers (see wrangler.toml) and routes each to its workflow.
const WORKFLOWS = {
  "7 * * * *": "update-gist.yml", // hourly: fuel prices -> stations.json
  "23 4 * * *": "update-brands.yml", // daily: OSM brands -> osm-map.json
};

const b64url = (buf) =>
  btoa(String.fromCodePoint(...new Uint8Array(buf)))
    .replaceAll("+", "-")
    .replaceAll("/", "_")
    .replaceAll("=", "");

async function importAppKey(pem) {
  const body = pem.replace(/-----[^-]+-----/g, "").replace(/\s+/g, "");
  const der = Uint8Array.from(atob(body), (c) => c.codePointAt(0));
  return crypto.subtle.importKey(
    "pkcs8",
    der,
    { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
    false,
    ["sign"],
  );
}

/** Mint a short-lived JWT authenticating as the GitHub App itself. */
async function appJwt(env) {
  const now = Math.floor(Date.now() / 1000);
  const header = b64url(new TextEncoder().encode('{"alg":"RS256","typ":"JWT"}'));
  const payload = b64url(
    new TextEncoder().encode(
      JSON.stringify({ iat: now - 60, exp: now + 540, iss: env.GITHUB_APP_ID }),
    ),
  );
  const key = await importAppKey(env.GITHUB_APP_KEY);
  const sig = await crypto.subtle.sign(
    "RSASSA-PKCS1-v1_5",
    key,
    new TextEncoder().encode(`${header}.${payload}`),
  );
  return `${header}.${payload}.${b64url(sig)}`;
}

const ghHeaders = (auth) => ({
  Authorization: `Bearer ${auth}`,
  Accept: "application/vnd.github+json",
  "X-GitHub-Api-Version": "2022-11-28",
  "User-Agent": "fuel2gist-dispatcher",
});

async function dispatchWorkflow(env, workflow) {
  const jwt = await appJwt(env);

  // Exchange the app JWT for an installation access token (~1h validity).
  const tokRes = await fetch(
    `${API}/app/installations/${env.GITHUB_INSTALLATION_ID}/access_tokens`,
    { method: "POST", headers: ghHeaders(jwt) },
  );
  if (!tokRes.ok) {
    throw new Error(`access_token: HTTP ${tokRes.status} ${await tokRes.text()}`);
  }
  const { token } = await tokRes.json();

  // Fire the workflow_dispatch event.
  const res = await fetch(
    `${API}/repos/${REPO}/actions/workflows/${workflow}/dispatches`,
    {
      method: "POST",
      headers: { ...ghHeaders(token), "Content-Type": "application/json" },
      body: JSON.stringify({ ref: "main" }),
    },
  );
  if (res.status !== 204) {
    throw new Error(`dispatch: HTTP ${res.status} ${await res.text()}`);
  }
  return "dispatched";
}

export default {
  async scheduled(event, env) {
    // Errors surface as failed entries in the Cron Events log.
    const workflow = WORKFLOWS[event.cron];
    if (workflow) await dispatchWorkflow(env, workflow);
  },

  // Manual trigger for testing: GET / -> prices workflow,
  // GET /brands -> brands map workflow. Remove this handler if you don't
  // want an open trigger endpoint.
  async fetch(request, env) {
    const path = new URL(request.url).pathname;
    const workflow =
      path === "/"
        ? WORKFLOWS["7 * * * *"]
        : path === "/brands"
          ? WORKFLOWS["23 4 * * *"]
          : null;
    if (!workflow) return new Response("not found", { status: 404 });
    try {
      return new Response(await dispatchWorkflow(env, workflow));
    } catch (err) {
      return new Response(String(err), { status: 500 });
    }
  },
};
