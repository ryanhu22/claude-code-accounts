// `ccm login <name> --codex`: the same shape as the Claude flow, on port 1455
// and the /auth/callback path the Codex client is registered for.
import { expect } from 'e2e';
import { needsCodexPort, test } from './signins.ts';

test('a Codex sign-in lands on "Signed in." and the account shows in ccm list', async ({
  signins,
  app,
  screen,
  browser,
}) => {
  await needsCodexPort();
  const signin = await signins.start({ account: 'gpt', email: 'gpt@example.com', codex: true });
  expect(signin.callback_url).toBe('http://localhost:1455/auth/callback');
  await app.open(signin.authorize_url);
  await expect(screen.getByRole('heading', 'Authorize Codex?')).toBeVisible();
  await screen.getByRole('link', 'Authorize').tap();

  await expect(browser).toHaveURL(/^http:\/\/localhost:1455\/auth\/callback\?/);
  await expect(screen.getByRole('heading', 'Signed in.')).toBeVisible();
  await expect(screen.getByText('“gpt” is signed in as gpt@example.com')).toBeVisible();

  const outcome = await signins.outcome(signin.id);
  expect(outcome.returncode).toBe(0);
  expect(outcome.signed_in).toBe(true);
  const list = await signins.ccm(signin.id, 'list');
  expect(list.plain).toContain('gpt codex  gpt@example.com · Pro Lite');
});

test('a denied Codex sign-in says so on the page and in ccm', async ({ signins, app, screen }) => {
  await needsCodexPort();
  const signin = await signins.start({ account: 'gpt', codex: true, deny: true });
  await app.open(signin.authorize_url);
  await screen.getByRole('link', 'Authorize').tap();

  await expect(screen.getByRole('heading', 'The sign-in did not finish.')).toBeVisible();
  await expect(screen.getByText('The sign-in page reported that the request was not allowed.')).toBeVisible();
  await expect(screen.getByText('Signed in')).toHaveCount(0);

  const outcome = await signins.outcome(signin.id);
  expect(outcome.returncode).toBe(1);
  expect(outcome.signed_in).toBe(false);
  expect(outcome.plain_err).toContain('was not allowed');
});

test('the Codex callback refuses a forged state, then takes the real redirect', async ({
  signins,
  app,
  screen,
}) => {
  await needsCodexPort();
  const signin = await signins.start({ account: 'gpt', email: 'gpt@example.com', codex: true });
  await app.open(`${signin.callback_url}?code=forged&state=not-this-sign-in`);
  await expect(screen.getByRole('heading', 'This page does not belong to the sign-in in progress.')).toBeVisible();

  await app.open(signin.authorize_url);
  await screen.getByRole('link', 'Authorize').tap();
  await expect(screen.getByRole('heading', 'Signed in.')).toBeVisible();
  const outcome = await signins.outcome(signin.id);
  expect(outcome.returncode).toBe(0);
  expect(outcome.plain).toContain('“gpt” is signed in as gpt@example.com');
});

test('a refused Codex code is reported on the page', async ({ signins, app, screen }) => {
  await needsCodexPort();
  const signin = await signins.start({ account: 'gpt', codex: true, expired_code: true });
  await app.open(signin.authorize_url);
  await screen.getByRole('link', 'Authorize').tap();

  await expect(screen.getByRole('heading', 'The sign-in did not finish.')).toBeVisible();
  await expect(screen.getByText(/the code was refused or could not be exchanged/)).toBeVisible();
  const outcome = await signins.outcome(signin.id);
  expect(outcome.returncode).toBe(1);
  expect(outcome.signed_in).toBe(false);
});

test('a browser signed in to another Codex account is accepted, and ccm says the account changed', async ({
  signins,
  app,
  screen,
}) => {
  await needsCodexPort();
  const signin = await signins.start({
    account: 'gpt',
    email: 'gpt@example.com',
    codex: true,
    seeded: 'gpt@example.com',
    browser: 'other@example.com',
  });
  await app.open(signin.authorize_url);
  await expect(screen.getByText('other@example.com')).toBeVisible();
  await screen.getByRole('link', 'Authorize').tap();

  await expect(screen.getByRole('heading', 'Signed in.')).toBeVisible();
  const warning =
    '“gpt” is now signed in as other@example.com, but it used to be gpt@example.com. ' +
    'If that is wrong, sign in again in a private window.';
  await expect(screen.getByText(warning)).toBeVisible();
  const outcome = await signins.outcome(signin.id);
  expect(outcome.returncode).toBe(0);
  expect(outcome.plain).toContain(warning);
});

test('with port 1455 busy, ccm says so and opens no browser', async ({ signins }) => {
  await needsCodexPort();
  const exited = await signins.startExpectingExit({ account: 'gpt', codex: true, busy_port: true });
  expect(exited.returncode).toBe(1);
  expect(exited.plain_err).toContain('port 1455 is in use');
});
