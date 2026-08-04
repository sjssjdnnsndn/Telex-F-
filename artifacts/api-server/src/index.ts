import { spawn, type ChildProcess } from "node:child_process";
import path from "node:path";
import app from "./app";
import { logger } from "./lib/logger";

const rawPort = process.env["PORT"];

if (!rawPort) {
  throw new Error(
    "PORT environment variable is required but was not provided.",
  );
}

const port = Number(rawPort);

if (Number.isNaN(port) || port <= 0) {
  throw new Error(`Invalid PORT value: "${rawPort}"`);
}

let botProcess: ChildProcess | undefined;

function startProductionBot(): void {
  if (process.env["RUN_TELEGRAM_BOT"] !== "true") return;

  const runBotPath = path.resolve(process.cwd(), "run-bot.sh");
  botProcess = spawn("bash", [runBotPath], {
    cwd: process.cwd(),
    env: { ...process.env, PORT: process.env["BOT_PORT"] ?? "8000" },
    stdio: "inherit",
  });

  botProcess.on("error", (err) => {
    logger.error({ err }, "Failed to start Telegram bot child process");
  });
  botProcess.on("exit", (code, signal) => {
    if (code === 0 || signal === "SIGTERM" || signal === "SIGINT") return;
    logger.error({ code, signal }, "Telegram bot child process exited unexpectedly");
  });
  logger.info({ runBotPath }, "Production Telegram bot process started");
}

const server = app.listen(port, (err) => {
  if (err) {
    logger.error({ err }, "Error listening on port");
    process.exit(1);
  }

  logger.info({ port }, "Server listening");
  logger.info("UPI API and startup health routes are ready");
  startProductionBot();
});

function shutdown(signal: string): void {
  logger.info({ signal }, "Shutting down API server");
  botProcess?.kill("SIGTERM");
  server.close(() => process.exit(0));
}

process.once("SIGTERM", () => shutdown("SIGTERM"));
process.once("SIGINT", () => shutdown("SIGINT"));
