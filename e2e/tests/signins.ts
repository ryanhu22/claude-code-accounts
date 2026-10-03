// The `signins` fixture: sandboxed `ccm login` runs, handed out by
// tests/e2e/signin_service.py (the app the config starts).
import { createServer } from 'node:net';
import { test as base } from '@e2e-dev/web';
import { chromium, type Page } from 'playwright';

export interface SignInOptions {
  account?: string;
  email?: string;
  tier?: string;
  plan?: string;
  codex?: boolean;
  /** The Authorize press comes back as access_denied. */
  deny?: boolean;
  /** The email the browser is signed in as, when it is another account. */
  browser?: string;
  /** The email the slot held before this sign-in. */
  seeded?: string;
  /** The token endpoint refuses the code as expired. */
  expired_code?: boolean;
  /** Seconds the token exchange takes. */
  slow_token?: number;
  /** Something else holds port 1455 before ccm starts. */
  busy_port?: boolean;
  /** Share the sandbox and fake server of an earlier sign-in. */
  sandbox_of?: string;
}

export interface Started {
  id: string;
  account: string;
  email: string;
  codex: boolean;
  authorize_url: string;
  fake_server: string;
  callback_url: string;
  sandbox: string;
}

export interface Exited {
  id: string;
  exited: true;
  returncode: number | null;
  stdout: string;
  stderr: string;
  plain: string;
  plain_err: string;
}

export interface Outcome {
  done: boolean;
  returncode: number | null;
  signed_in: boolean;
  /** ccm's stdout and stderr with the colours stripped. */
  plain: string;
  plain_err: string;
}

export interface CcmResult {
  returncode: number;
  plain: string;
  plain_err: string;
}

export class SignIns {
  private readonly created: string[] = [];

  constructor(private readonly base: string) {}

  /** Start `ccm login` in a fresh sandbox and get the URLs the browser needs. */
  async start(options: SignInOptions = {}): Promise<Started> {
    const started = await this.call<Started | Exited>('POST', '/signins', options);
    this.created.push(started.id);
    if ('exited' in started) {
      throw new Error(`ccm exited ${started.returncode} before opening a browser:\n${started.stderr}`);
    }
    return started;
  }

  /** Start one that is expected to exit before it opens a browser. */
  async startExpectingExit(options: SignInOptions): Promise<Exited> {
    const started = await this.call<Started | Exited>('POST', '/signins', options);
    this.created.push(started.id);
    if (!('exited' in started)) throw new Error(`ccm opened a browser at ${started.authorize_url}`);
    return started;
  }

  /** How ccm ended; waits up to `wait` seconds for it to exit. */
  async outcome(id: string, wait = 30): Promise<Outcome> {
    const outcome = await this.call<Outcome>('GET', `/signins/${id}?wait=${wait}`);
    if (!outcome.done) throw new Error(`ccm did not exit within ${wait}s`);
    return outcome;
  }

  /** Run another ccm command in that sign-in's sandbox. */
  async ccm(id: string, ...args: string[]): Promise<CcmResult> {
    return this.call<CcmResult>('POST', `/signins/${id}/ccm`, { args });
  }

  async close(): Promise<void> {
    // Shared sandboxes belong to the sign-in created first, so drop the
    // latest first.
    for (const id of this.created.reverse()) {
      await this.call('DELETE', `/signins/${id}`).catch(() => undefined);
    }
  }

  private async call<T>(method: string, path: string, body?: unknown): Promise<T> {
    const response = await fetch(new URL(path, this.base), {
      method,
      headers: { 'content-type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const data = (await response.json()) as T & { error?: string };
    if (!response.ok) throw new Error(`${method} ${path}: ${response.status} ${data.error ?? ''}`);
    return data;
  }
}

export const test = base.extend<{ signins: SignIns }>({
  signins: async ({ app }, use) => {
    const signins = new SignIns(app.baseUrl!);
    await use(signins);
    await signins.close();
  },
});

/** The HTML of a page as the server sends it: what a browser without JavaScript gets. */
export async function rawPage(url: string): Promise<{ status: number; html: string }> {
  const response = await fetch(url, { redirect: 'follow' });
  return { status: response.status, html: await response.text() };
}

/** Where a URL redirects to, without following it: the callback URL with its code. */
export async function redirectOf(url: string): Promise<string> {
  const response = await fetch(url, { redirect: 'manual' });
  const location = response.headers.get('location');
  if (response.status !== 302 || !location) throw new Error(`${url} did not redirect (${response.status})`);
  return location;
}

// Words that promise ease or sell instead of telling the user what happened.
// Shipped copy must not use them (see the STE rules in CLAUDE.md).
export const BANNED_WORDS =
  /\b(seamless|effortless|robust|powerful|cutting-edge|next-level|game-changer|supercharge|revolutionize|unlock|elevate|empower|delve|harness|streamline|leverage|simply|just|easily|quickly)\b/i;

/** Every sentence of `text`, for the length rule: at most 20 words each. */
export function sentences(text: string): string[] {
  return text
    .split(/(?<=[.!?])\s+/)
    .map((s) => s.trim())
    .filter(Boolean);
}

/**
 * Sign in through a browser that prefers `scheme`, and hand the landed page
 * to `fn`. The runner's browser has no theme switch, so this is Playwright
 * directly: a headless Chromium of its own, closed before this returns.
 */
export async function landedIn<T>(
  scheme: 'dark' | 'light',
  authorizeUrl: string,
  fn: (page: Page) => Promise<T>,
): Promise<T> {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ colorScheme: scheme });
    await page.goto(authorizeUrl);
    await page.locator('#authorize').click();
    await page.getByRole('heading').waitFor();
    return await fn(page);
  } finally {
    await browser.close();
  }
}

/** Whether this process could bind the port now, the way ccm's callback server does. */
export function portFree(port: number): Promise<boolean> {
  return new Promise((resolve) => {
    const server = createServer();
    server.once('error', () => resolve(false));
    server.listen({ port, host: '127.0.0.1' }, () => server.close(() => resolve(true)));
  });
}

/** Codex sign-ins bind port 1455: skip when something else holds it. */
export async function needsCodexPort(): Promise<void> {
  test.skip(!(await portFree(1455)), 'port 1455 is in use (a Codex sign-in?)');
}
