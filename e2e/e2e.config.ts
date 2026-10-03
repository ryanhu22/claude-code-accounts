import type { E2EConfig } from 'e2e';
import { web } from '@e2e-dev/web';
import { chatgpt } from 'e2e/oauth/chatgpt';

// The browser suite for ccm's one browser surface: signing in. The app under
// test is tests/e2e/signin_service.py, which hands out sandboxed `ccm login`
// runs against a fake Anthropic and OpenAI server. Nothing here reaches a
// real host, keychain or home directory (tests/e2e/README.md).
export const engine = web();

export default {
  agents: {
    default: {
      // The user's ChatGPT subscription: `npx e2e login openai` once.
      model: chatgpt('gpt-6-luna'),
      system:
        'You are a careful QA engineer checking the browser pages of a small ' +
        'command-line tool. Verify every outcome on the page in front of you.',
    },
  },
  targets: [
    {
      name: 'signin',
      engine,
      app: {
        url: 'http://127.0.0.1:0',
        command: {
          executable: 'uv',
          args: ['run', 'python', 'tests/e2e/signin_service.py', '--port', '{port}'],
          cwd: '..',
          log: '.e2e/logs/signin-service.log',
        },
      },
    },
  ],
  // A Codex sign-in binds port 1455, as the real client does, so two of
  // them cannot run at once.
  workers: 1,
  timeout: 90_000,
} satisfies E2EConfig;
