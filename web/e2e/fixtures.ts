import {
  spawn,
  type ChildProcessWithoutNullStreams,
} from "node:child_process";
import { randomUUID } from "node:crypto";
import { existsSync, mkdirSync, mkdtempSync, rmSync } from "node:fs";
import { createServer } from "node:net";
import { dirname, join, resolve } from "node:path";

import { expect, test as base, type Page } from "@playwright/test";

import {
  resolveRequiredPythonExecutable,
  withoutPythonOverrides,
} from "./python-runtime";

type WorkerFixtures = {
  appServer: AppServer;
  appUrl: string;
};

export type AppServer = {
  url: string;
  dataRoot: string;
  dbPath: string;
  start: () => Promise<void>;
  stop: () => Promise<void>;
  restart: () => Promise<void>;
};

const repositoryRoot = resolve(import.meta.dirname, "../..");
const pythonExecutable = resolveRequiredPythonExecutable(repositoryRoot);
const frontendIndex = resolve(repositoryRoot, "web/dist/index.html");

async function reservePort(): Promise<number> {
  return new Promise((resolvePort, reject) => {
    const server = createServer();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      if (!address || typeof address === "string") {
        server.close();
        reject(new Error("Could not allocate an E2E server port"));
        return;
      }
      const port = address.port;
      server.close((error) => (error ? reject(error) : resolvePort(port)));
    });
  });
}

async function waitForServer(
  process: ChildProcessWithoutNullStreams,
  url: string,
  logs: string[],
): Promise<void> {
  const deadline = Date.now() + 45_000;
  while (Date.now() < deadline) {
    if (process.exitCode !== null) {
      throw new Error(`Uvicorn exited before readiness:\n${logs.join("").slice(-4_000)}`);
    }
    try {
      const response = await fetch(`${url}/healthz`);
      if (response.ok) {
        return;
      }
    } catch {
      // The listener is not ready yet.
    }
    await new Promise((resolveDelay) => setTimeout(resolveDelay, 100));
  }
  throw new Error(`Timed out waiting for Uvicorn:\n${logs.join("").slice(-4_000)}`);
}

async function stopProcess(process: ChildProcessWithoutNullStreams): Promise<void> {
  if (process.exitCode !== null) {
    return;
  }
  const exited = new Promise<void>((resolveExit) => process.once("exit", () => resolveExit()));
  process.kill();
  await Promise.race([
    exited,
    new Promise<void>((resolveTimeout) => setTimeout(resolveTimeout, 5_000)),
  ]);
  if (process.exitCode === null) {
    process.kill("SIGKILL");
    await exited;
  }
}

export const test = base.extend<object, WorkerFixtures>({
  appServer: [
    async ({ browserName }, use) => {
      if (browserName !== "chromium") {
        throw new Error(`Task 6 supports exactly one browser: received ${browserName}`);
      }
      if (!existsSync(frontendIndex)) {
        throw new Error("Build web/dist before running Playwright: npm run build");
      }

      const expectedParent = resolve(repositoryRoot, ".runtime");
      mkdirSync(expectedParent, { recursive: true });
      const dataRoot = mkdtempSync(join(expectedParent, "zhiyan-playwright-"));
      if (dirname(resolve(dataRoot)) !== expectedParent) {
        throw new Error(`Unexpected E2E data root: ${dataRoot}`);
      }
      const port = await reservePort();
      const appUrl = `http://127.0.0.1:${port}`;
      const logs: string[] = [];
      let serverProcess: ChildProcessWithoutNullStreams | undefined;

      const stop = async () => {
        const ownedProcess = serverProcess;
        serverProcess = undefined;
        if (ownedProcess) {
          await stopProcess(ownedProcess);
        }
      };
      const start = async () => {
        if (serverProcess?.exitCode === null) {
          return;
        }
        logs.length = 0;
        serverProcess = spawn(
          pythonExecutable,
          [
            "-m",
            "uvicorn",
            "server:app",
            "--host",
            "127.0.0.1",
            "--port",
            String(port),
          ],
          {
            cwd: repositoryRoot,
            env: {
              ...withoutPythonOverrides(globalThis.process.env),
              PDF_ASSISTANT_DATA_DIR: dataRoot,
              PYTHONUNBUFFERED: "1",
            },
          },
        );
        serverProcess.stdout.on("data", (chunk: Buffer) => logs.push(chunk.toString()));
        serverProcess.stderr.on("data", (chunk: Buffer) => logs.push(chunk.toString()));
        try {
          await waitForServer(serverProcess, appUrl, logs);
        } catch (error) {
          await stop();
          throw error;
        }
      };
      const restart = async () => {
        await stop();
        await start();
      };

      try {
        await start();
        await use({ url: appUrl, dataRoot, dbPath: join(dataRoot, "app.db"), start, stop, restart });
      } finally {
        await stop();
        await new Promise((resolveDelay) => setTimeout(resolveDelay, 500));
        rmSync(dataRoot, {
          force: true,
          maxRetries: 10,
          recursive: true,
          retryDelay: 250,
        });
      }
    },
    { scope: "worker", timeout: 60_000 },
  ],
  appUrl: [
    async ({ appServer }, use) => {
      await use(appServer.url);
    },
    { scope: "worker" },
  ],
});

export const qaTest = base.extend<object, WorkerFixtures>({
  appServer: [
    async ({ browserName }, use) => {
      if (browserName !== "chromium") {
        throw new Error(`QA acceptance supports exactly one browser: received ${browserName}`);
      }
      if (!existsSync(frontendIndex)) {
        throw new Error("Build web/dist before running Playwright: npm run build");
      }

      const expectedParent = resolve(repositoryRoot, ".runtime");
      mkdirSync(expectedParent, { recursive: true });
      const dataRoot = mkdtempSync(join(expectedParent, "zhiyan-qa-playwright-"));
      if (dirname(resolve(dataRoot)) !== expectedParent) {
        throw new Error(`Unexpected QA E2E data root: ${dataRoot}`);
      }
      const port = await reservePort();
      const appUrl = `http://127.0.0.1:${port}`;
      const logs: string[] = [];
      let serverProcess: ChildProcessWithoutNullStreams | undefined;

      const stop = async () => {
        const ownedProcess = serverProcess;
        serverProcess = undefined;
        if (ownedProcess) await stopProcess(ownedProcess);
      };
      const start = async () => {
        if (serverProcess?.exitCode === null) return;
        logs.length = 0;
        serverProcess = spawn(
          pythonExecutable,
          [resolve(repositoryRoot, "web/e2e/qa-runtime.py"), "--host", "127.0.0.1", "--port", String(port)],
          {
            cwd: repositoryRoot,
            env: {
              ...withoutPythonOverrides(globalThis.process.env),
              PDF_ASSISTANT_DATA_DIR: dataRoot,
              PYTHONUNBUFFERED: "1",
              QA_ROUTE_ENABLED: "true",
            },
          },
        );
        serverProcess.stdout.on("data", (chunk: Buffer) => logs.push(chunk.toString()));
        serverProcess.stderr.on("data", (chunk: Buffer) => logs.push(chunk.toString()));
        try {
          await waitForServer(serverProcess, appUrl, logs);
        } catch (error) {
          await stop();
          throw error;
        }
      };
      const restart = async () => { await stop(); await start(); };

      try {
        await start();
        await use({ url: appUrl, dataRoot, dbPath: join(dataRoot, "app.db"), start, stop, restart });
      } finally {
        await stop();
        await new Promise((resolveDelay) => setTimeout(resolveDelay, 500));
        rmSync(dataRoot, { force: true, maxRetries: 10, recursive: true, retryDelay: 250 });
      }
    },
    { scope: "worker", timeout: 60_000 },
  ],
  appUrl: [
    async ({ appServer }, use) => { await use(appServer.url); },
    { scope: "worker" },
  ],
});

export const notesTest = base.extend<object, WorkerFixtures>({
  appServer: [
    async ({ browserName }, use) => {
      if (browserName !== "chromium") {
        throw new Error(`Notes acceptance supports exactly one browser: received ${browserName}`);
      }
      if (!existsSync(frontendIndex)) {
        throw new Error("Build web/dist before running Playwright: npm run build");
      }

      const expectedParent = resolve(repositoryRoot, ".runtime");
      mkdirSync(expectedParent, { recursive: true });
      const dataRoot = mkdtempSync(join(expectedParent, "zhiyan-notes-playwright-"));
      if (dirname(resolve(dataRoot)) !== expectedParent) {
        throw new Error(`Unexpected Notes E2E data root: ${dataRoot}`);
      }
      const port = await reservePort();
      const appUrl = `http://127.0.0.1:${port}`;
      const logs: string[] = [];
      let serverProcess: ChildProcessWithoutNullStreams | undefined;

      const stop = async () => {
        const ownedProcess = serverProcess;
        serverProcess = undefined;
        if (ownedProcess) await stopProcess(ownedProcess);
      };
      const start = async () => {
        if (serverProcess?.exitCode === null) return;
        logs.length = 0;
        serverProcess = spawn(
          pythonExecutable,
          [resolve(repositoryRoot, "web/e2e/notes-runtime.py"), "--host", "127.0.0.1", "--port", String(port)],
          {
            cwd: repositoryRoot,
            env: {
              ...withoutPythonOverrides(globalThis.process.env),
              PDF_ASSISTANT_DATA_DIR: dataRoot,
              PYTHONUNBUFFERED: "1",
              NOTES_ROUTE_ENABLED: "true",
              QA_ROUTE_ENABLED: "true",
            },
          },
        );
        serverProcess.stdout.on("data", (chunk: Buffer) => logs.push(chunk.toString()));
        serverProcess.stderr.on("data", (chunk: Buffer) => logs.push(chunk.toString()));
        try {
          await waitForServer(serverProcess, appUrl, logs);
        } catch (error) {
          await stop();
          throw error;
        }
      };
      const restart = async () => { await stop(); await start(); };

      try {
        await start();
        await use({ url: appUrl, dataRoot, dbPath: join(dataRoot, "app.db"), start, stop, restart });
      } finally {
        await stop();
        await new Promise((resolveDelay) => setTimeout(resolveDelay, 500));
        rmSync(dataRoot, { force: true, maxRetries: 10, recursive: true, retryDelay: 250 });
      }
    },
    { scope: "worker", timeout: 60_000 },
  ],
  appUrl: [
    async ({ appServer }, use) => { await use(appServer.url); },
    { scope: "worker" },
  ],
});

export { expect };

export function uniqueUsername(prefix: string): string {
  return `${prefix}_${randomUUID().replaceAll("-", "").slice(0, 12)}`;
}

export async function registerUser(
  page: Page,
  appUrl: string,
  username: string,
): Promise<void> {
  await page.goto(`${appUrl}/register`);
  await expect(page.getByRole("heading", { level: 1, name: "注册" })).toBeVisible();
  await page.getByLabel("用户名").fill(username);
  await page.getByLabel("密码", { exact: true }).fill("e2e-only-passphrase");
  await page.getByRole("button", { name: "注册", exact: true }).click();
  await expect(page).toHaveURL(`${appUrl}/overview`, { timeout: 30_000 });
  await expect(page.getByRole("heading", { level: 1, name: `你好，${username.slice(0, 32)}` })).toBeVisible();
}

export async function openMore(page: Page, projectName: string) {
  const name = projectName === "mobile" ? "更多" : "更多操作";
  const trigger = page.getByRole("button", { name, exact: true });
  await trigger.click();
  await expect(page.getByRole("dialog", { name: "更多" })).toBeVisible();
  return trigger;
}

export async function settleVisuals(page: Page): Promise<void> {
  await page.addStyleTag({
    content:
      "*, *::before, *::after { animation: none !important; transition: none !important; caret-color: transparent !important; }",
  });
  await page.evaluate(async () => {
    await document.fonts.ready;
    await Promise.all(
      Array.from(document.images).map((image) => {
        if (image.complete) {
          return image.decode().catch(() => undefined);
        }
        return new Promise<void>((resolveImage) => {
          image.addEventListener("load", () => resolveImage(), { once: true });
          image.addEventListener("error", () => resolveImage(), { once: true });
        });
      }),
    );
    await new Promise<void>((resolveFrame) =>
      requestAnimationFrame(() => requestAnimationFrame(() => resolveFrame())),
    );
  });
}
