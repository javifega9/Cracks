const env = require("../config/env");

const levels = {
  error: 0,
  warn: 1,
  info: 2,
  debug: 3
};

function shouldLog(level) {
  const current = levels[env.logLevel] ?? levels.info;
  return (levels[level] ?? levels.info) <= current;
}

function log(level, message, meta) {
  if (!shouldLog(level)) {
    return;
  }

  const prefix = `[${new Date().toISOString()}] [${level.toUpperCase()}]`;
  if (meta !== undefined) {
    console[level === "debug" ? "log" : level](`${prefix} ${message}`, meta);
    return;
  }
  console[level === "debug" ? "log" : level](`${prefix} ${message}`);
}

module.exports = {
  error: (message, meta) => log("error", message, meta),
  warn: (message, meta) => log("warn", message, meta),
  info: (message, meta) => log("info", message, meta),
  debug: (message, meta) => log("debug", message, meta)
};
