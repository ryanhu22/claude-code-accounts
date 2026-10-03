// `ccm login <name>`: the browser authorizes on the (fake) Claude sign-in
// page and lands on ccm's own callback page, which reports the outcome.
import { expect } from 'e2e';
import { rawPage, redirectOf, test } from './signins.ts';

test('a Claude sign-in lands on "Signed in." and the account shows in ccm list', async ({
  signins,
  app,
  agent,
  screen,
  browser,
}) => {
  const signin = await signins.start({ account: 'work', email: 'work@example.com' });
  await app.open(signin.authorize_url);
  await expect(screen.getByRole('heading', 'Authorize Claude Code?')).toBeVisible();
  await expect(screen.getByText('work@example.com')).toBeVisible();

  await agent.act('allow the sign-in');

  await expect(browser).toHaveURL(new RegExp(`^${signin.callback_url}\\?`));
  await expect(screen.getByRole('heading', 'Signed in.')).toBeVisible();
  await expect(screen.getByText('“work” is signed in as work@example.com')).toBeVisible();
  await expect(screen.getByText('You can close this tab and go back to the app.')).toBeVisible();
  await agent.assert('the page says the sign-in succeeded and tells the user to go back to the app');

  const outcome = await signins.outcome(signin.id);
  expect(outcome.returncode).toBe(0);
  expect(outcome.signed_in).toBe(true);
  expect(outcome.plain).toContain('“work” is signed in as work@example.com');

  const list = await signins.ccm(signin.id, 'list');
  expect(list.returncode).toBe(0);
  expect(list.plain).toContain('work  work@example.com · Max 5x');
});

test('a denied sign-in says so on the page and in ccm, with no code to paste', async ({
  signins,
  app,
  screen,
}) => {
  const signin = await signins.start({ account: 'work', deny: true });
  await app.open(signin.authorize_url);
  await screen.getByRole('link', 'Authorize').tap();

  await expect(screen.getByRole('heading', 'The sign-in did not finish.')).toBeVisible();
  await expect(screen.getByText('The sign-in page reported that the request was not allowed.')).toBeVisible();
  await expect(screen.getByText('To try again, go back to the app and start the sign-in again.')).toBeVisible();
  await expect(screen.getByText('Signed in')).toHaveCount(0);

  const outcome = await signins.outcome(signin.id);
  expect(outcome.returncode).toBe(1);
  expect(outcome.signed_in).toBe(false);
  expect(outcome.plain_err).toContain('was not allowed');
  // A refusal is final: ccm must not then ask for a code the page never showed.
  expect(outcome.plain).not.toContain('Paste the code');
});

test('a code the token endpoint refuses is reported, not shown as signed in', async ({
  signins,
  app,
  screen,
}) => {
  const signin = await signins.start({ account: 'work', expired_code: true });
  await app.open(signin.authorize_url);
  await screen.getByRole('link', 'Authorize').tap();

  await expect(screen.getByRole('heading', 'The sign-in did not finish.')).toBeVisible();
  await expect(screen.getByText(/the code was refused \(Authorization code expired\)/)).toBeVisible();
  await expect(screen.getByText('Signed in')).toHaveCount(0);

  const outcome = await signins.outcome(signin.id);
  expect(outcome.returncode).toBe(1);
  expect(outcome.signed_in).toBe(false);
  expect(outcome.plain_err).toContain('Authorization code expired');
});

test('the callback refuses a forged state and the real sign-in still goes through', async ({
  signins,
  app,
  screen,
}) => {
  const signin = await signins.start({ account: 'work', email: 'work@example.com' });

  // A request that is not the redirect: someone else's state, no state, another path.
  await app.open(`${signin.callback_url}?code=forged&state=not-this-sign-in`);
  await expect(screen.getByRole('heading', 'This page does not belong to the sign-in in progress.')).toBeVisible();
  await expect(screen.getByText('To sign in, go back to the app and start again.')).toBeVisible();
  await app.open(`${signin.callback_url}?code=forged`);
  await expect(screen.getByRole('heading', 'This page does not belong to the sign-in in progress.')).toBeVisible();
  await app.open(`${new URL(signin.callback_url).origin}/favicon.ico`);
  await expect(screen.getByRole('heading', 'Not found.')).toBeVisible();

  // None of that consumed the attempt.
  await app.open(signin.authorize_url);
  await screen.getByRole('link', 'Authorize').tap();
  await expect(screen.getByRole('heading', 'Signed in.')).toBeVisible();

  const outcome = await signins.outcome(signin.id);
  expect(outcome.returncode).toBe(0);
  expect(outcome.plain).toContain('“work” is signed in as work@example.com');
});

test('the callback visited twice during the exchange shows the same outcome, and nothing after ccm exits', async ({
  signins,
  app,
  screen,
}) => {
  // The exchange takes three seconds. The browser lands on the callback and
  // waits; a second visit of the same redirect (a reload, a second tab)
  // arrives while ccm is still finishing, and must get the same page, not a
  // second exchange of the same code.
  const signin = await signins.start({ account: 'work', email: 'work@example.com', slow_token: 3 });
  await app.open(signin.authorize_url);
  const approve = await screen.getByRole('link', 'Authorize').getAttribute('href');
  const landing = await redirectOf(new URL(approve!, signin.fake_server).toString());
  expect(landing.startsWith(`${signin.callback_url}?`)).toBe(true);

  const [, second] = await Promise.all([
    app.open(landing),
    new Promise((resolve) => setTimeout(resolve, 500)).then(() => rawPage(landing)),
  ]);
  await expect(screen.getByRole('heading', 'Signed in.')).toBeVisible();
  await expect(screen.getByText('“work” is signed in as work@example.com')).toBeVisible();
  expect(second.status).toBe(200);
  expect(second.html).toContain('<h2>Signed in.</h2>');
  expect(second.html).toContain('“work” is signed in as work@example.com');

  const outcome = await signins.outcome(signin.id);
  expect(outcome.returncode).toBe(0);
  expect(outcome.signed_in).toBe(true);
  // One exchange: ccm reported the sign-in once.
  expect(outcome.plain.split('is signed in as').length - 1).toBe(1);

  // Once ccm has exited, its port is closed: a late visit gets no page at all,
  // and nothing on this machine answers for it.
  let reached = true;
  try {
    await app.open(`${signin.callback_url}?code=late&state=late`);
  } catch {
    reached = false;
  }
  expect(reached).toBe(false);
});

test('a browser signed in to another account is accepted, and ccm says the account changed', async ({
  signins,
  app,
  screen,
}) => {
  // The slot "work" held work@example.com; the browser is signed in as other@.
  const signin = await signins.start({
    account: 'work',
    email: 'work@example.com',
    seeded: 'work@example.com',
    browser: 'other@example.com',
  });
  await app.open(signin.authorize_url);
  await expect(screen.getByText('other@example.com')).toBeVisible();
  await screen.getByRole('link', 'Authorize').tap();

  await expect(screen.getByRole('heading', 'Signed in.')).toBeVisible();
  const warning =
    '“work” is now signed in as other@example.com, but it used to be work@example.com. ' +
    'If that is wrong, sign in again in a private window.';
  await expect(screen.getByText(warning)).toBeVisible();

  const outcome = await signins.outcome(signin.id);
  expect(outcome.returncode).toBe(0);
  expect(outcome.plain).toContain(warning);
  const list = await signins.ccm(signin.id, 'list');
  expect(list.plain).toContain('work  other@example.com');
});

test('a second account signs in next to the first', async ({ signins, app, screen }) => {
  const first = await signins.start({ account: 'work', email: 'work@example.com' });
  await app.open(first.authorize_url);
  await screen.getByRole('link', 'Authorize').tap();
  await expect(screen.getByRole('heading', 'Signed in.')).toBeVisible();
  expect((await signins.outcome(first.id)).signed_in).toBe(true);

  const second = await signins.start({
    account: 'personal',
    email: 'personal@example.com',
    tier: 'default_claude_pro',
    sandbox_of: first.id,
  });
  await app.open(second.authorize_url);
  await expect(screen.getByText('personal@example.com')).toBeVisible();
  await screen.getByRole('link', 'Authorize').tap();
  await expect(screen.getByText('“personal” is signed in as personal@example.com')).toBeVisible();
  expect((await signins.outcome(second.id)).signed_in).toBe(true);

  const list = await signins.ccm(first.id, 'list');
  expect(list.plain).toContain('work  work@example.com · Max 5x');
  expect(list.plain).toContain('personal  personal@example.com · Pro');
});
