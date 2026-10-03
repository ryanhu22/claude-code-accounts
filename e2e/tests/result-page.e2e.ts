// The pages ccm serves on its callback port: what they are made of and how
// they read. Every page is static HTML with no script and nothing fetched,
// follows the system theme, and uses short plain sentences.
import { expect } from 'e2e';
import { BANNED_WORDS, landedIn, rawPage, sentences, test } from './signins.ts';

/** What the page is made of, read from inside the browser. */
async function anatomy(browser: { evaluate<T>(fn: () => T): Promise<T> }) {
  return browser.evaluate(() => ({
    scripts: document.scripts.length,
    resources: performance.getEntriesByType('resource').length,
    external: document.querySelectorAll('[src], link[href], iframe, object, embed').length,
    html: document.documentElement.outerHTML,
    text: document.body.innerText,
    colorScheme: document.querySelector('meta[name="color-scheme"]')?.getAttribute('content') ?? '',
    background: getComputedStyle(document.body).backgroundColor,
    color: getComputedStyle(document.body).color,
  }));
}

/** WCAG contrast ratio of two `rgb(...)` strings, and the lighter one's luminance. */
function contrast(a: string, b: string): { ratio: number; luminance: [number, number] } {
  const luminance = (css: string): number => {
    const [r, g, b] = (css.match(/\d+(\.\d+)?/g) ?? ['0', '0', '0']).slice(0, 3).map((n) => {
      const c = Number(n) / 255;
      return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
    });
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  };
  const [la, lb] = [luminance(a), luminance(b)];
  const [hi, lo] = la > lb ? [la, lb] : [lb, la];
  return { ratio: (hi + 0.05) / (lo + 0.05), luminance: [la, lb] };
}

function checkCopy(text: string): void {
  expect(text).not.toContain('—');
  expect(text).not.toMatch(BANNED_WORDS);
  for (const sentence of sentences(text)) {
    expect(sentence.split(/\s+/).length).toBeLessThanOrEqual(20);
  }
}

test('the "Signed in." page is plain HTML: no script, nothing fetched, short sentences', async ({
  signins,
  app,
  screen,
  browser,
  agent,
}) => {
  const signin = await signins.start({ account: 'work', email: 'work@example.com' });
  await app.open(signin.authorize_url);
  await screen.getByRole('link', 'Authorize').tap();
  await expect(screen.getByRole('heading', 'Signed in.')).toBeVisible();

  const page = await anatomy(browser);
  expect(page.scripts).toBe(0);
  expect(page.external).toBe(0);
  expect(page.resources).toBe(0);
  expect(page.html).not.toMatch(/https?:/);
  expect(page.colorScheme).toBe('light dark');
  checkCopy(page.text);
  await agent.assert(
    'the page uses short plain sentences, says what happened, and tells the user what to do next',
  );
  expect((await signins.outcome(signin.id)).signed_in).toBe(true);
});

test('the failure page reads as plainly as the success page', async ({
  signins,
  app,
  screen,
  browser,
  agent,
}) => {
  const signin = await signins.start({ account: 'work', deny: true });
  await app.open(signin.authorize_url);
  await screen.getByRole('link', 'Authorize').tap();
  await expect(screen.getByRole('heading', 'The sign-in did not finish.')).toBeVisible();

  const page = await anatomy(browser);
  expect(page.scripts).toBe(0);
  expect(page.external).toBe(0);
  expect(page.html).not.toMatch(/https?:/);
  checkCopy(page.text);
  await agent.assert(
    'the page says the sign-in did not finish, gives the reason, and tells the user to go back to the app and try again',
  );

  expect((await signins.outcome(signin.id)).returncode).toBe(1);
});

test('the wrong-sign-in page reads plainly too', async ({ signins, app, screen, browser }) => {
  const signin = await signins.start({ account: 'work' });
  await app.open(`${signin.callback_url}?state=someone-elses`);
  await expect(screen.getByRole('heading', 'This page does not belong to the sign-in in progress.')).toBeVisible();
  const page = await anatomy(browser);
  expect(page.scripts).toBe(0);
  expect(page.external).toBe(0);
  checkCopy(page.text);
});

test('the pages render without JavaScript: the content is in the HTML the server sends', async ({
  signins,
  app,
  screen,
}) => {
  // Node's fetch runs no script: what it gets is what a browser without
  // JavaScript shows.
  const signin = await signins.start({ account: 'work', email: 'work@example.com' });
  await app.open(signin.authorize_url);
  const approve = await screen.getByRole('link', 'Authorize').getAttribute('href');
  const landed = await rawPage(new URL(approve!, signin.fake_server).toString());
  expect(landed.status).toBe(200);
  expect(landed.html.startsWith('<!doctype html>')).toBe(true);
  expect(landed.html).toContain('<h2>Signed in.</h2>');
  expect(landed.html).toContain('“work” is signed in as work@example.com');
  expect(landed.html).not.toContain('<script');
  expect(landed.html).not.toMatch(/https?:/);
  expect((await signins.outcome(signin.id)).signed_in).toBe(true);
});

test('the pages read in dark mode and in light mode', async ({ signins }) => {
  // The page sets no colours of its own: the browser paints its canvas and
  // text in the theme's colours. Read what it paints, not the body's
  // transparent background.
  const colours = (page: { evaluate<T>(fn: () => T): Promise<T> }) =>
    page.evaluate(() => {
      const probe = document.createElement('div');
      probe.style.cssText = 'background:Canvas;color:CanvasText';
      document.body.append(probe);
      const own = getComputedStyle(document.body);
      const painted = getComputedStyle(probe).backgroundColor;
      const result = {
        heading: document.querySelector('h2')?.textContent ?? '',
        background:
          own.backgroundColor === 'rgba(0, 0, 0, 0)' || own.backgroundColor === 'transparent'
            ? painted
            : own.backgroundColor,
        color: own.color,
      };
      probe.remove();
      return result;
    });

  const inDark = await signins.start({ account: 'work', email: 'work@example.com' });
  const dark = await landedIn('dark', inDark.authorize_url, colours);
  expect(dark.heading).toBe('Signed in.');
  const darkContrast = contrast(dark.background, dark.color);
  expect(darkContrast.ratio).toBeGreaterThanOrEqual(4.5);
  // A dark theme: the page's background is the dark one, not a white sheet.
  expect(darkContrast.luminance[0]).toBeLessThan(0.2);
  expect((await signins.outcome(inDark.id)).signed_in).toBe(true);

  const inLight = await signins.start({ account: 'home', email: 'home@example.com' });
  const light = await landedIn('light', inLight.authorize_url, colours);
  expect(light.heading).toBe('Signed in.');
  const lightContrast = contrast(light.background, light.color);
  expect(lightContrast.ratio).toBeGreaterThanOrEqual(4.5);
  expect(lightContrast.luminance[0]).toBeGreaterThan(0.8);
  expect((await signins.outcome(inLight.id)).signed_in).toBe(true);
});
