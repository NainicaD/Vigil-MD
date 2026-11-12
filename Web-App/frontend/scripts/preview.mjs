// Serves the built site (dist/) at http://localhost:4173 the way the API container does
// (Model-Development/chatbot/api/app.py add_site): files as files, /device as device.html,
// anything else as 404.html. With DEV_API_PROXY set (e.g. http://127.0.0.1:8000), /api is
// forwarded there. Development only; nothing here is part of the build.
import { createReadStream, statSync } from "node:fs";
import { createServer, request as httpRequest } from "node:http";
import { extname, join, resolve, sep } from "node:path";

const ROOT = resolve("dist");
const PORT = Number(process.env.PORT ?? 4173);
const PROXY = process.env.DEV_API_PROXY?.trim().replace(/\/+$/, "");
const TYPES = {
  ".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css",
  ".json": "application/json", ".txt": "text/plain; charset=utf-8", ".svg": "image/svg+xml",
  ".ico": "image/x-icon", ".png": "image/png", ".woff2": "font/woff2",
};

const isFile = (p) => {
  try {
    return statSync(p).isFile();
  } catch {
    return false;
  }
};

function send(res, file, status = 200) {
  res.writeHead(status, { "Content-Type": TYPES[extname(file)] ?? "application/octet-stream" });
  createReadStream(file).pipe(res);
}

createServer((req, res) => {
  const url = new URL(req.url ?? "/", "http://localhost");
  if (url.pathname === "/api" || url.pathname.startsWith("/api/")) {
    if (!PROXY) {
      res.writeHead(503, { "Content-Type": "application/json" });
      return res.end(JSON.stringify({ status: "error", text: "No API: set DEV_API_PROXY." }));
    }
    const target = new URL(req.url ?? "/", PROXY);
    const upstream = httpRequest(target, { method: req.method, headers: { ...req.headers, host: target.host } }, (r) => {
      res.writeHead(r.statusCode ?? 502, r.headers);
      r.pipe(res);
    });
    upstream.on("error", () => {
      res.writeHead(502);
      res.end();
    });
    return req.pipe(upstream);
  }

  const path = decodeURIComponent(url.pathname).replace(/^\/+/, "");
  const inRoot = (p) => p === ROOT || p.startsWith(ROOT + sep);
  const file = resolve(join(ROOT, path));
  const page = resolve(join(ROOT, `${path.replace(/\/+$/, "")}.html`));
  if (!path) return send(res, join(ROOT, "index.html"));
  if (inRoot(file) && isFile(file)) return send(res, file);
  if (inRoot(page) && isFile(page)) return send(res, page);
  return send(res, join(ROOT, "404.html"), 404);
}).listen(PORT, () => {
  process.stdout.write(`Serving dist/ at http://localhost:${PORT}${PROXY ? ` (API: ${PROXY})` : ""}\n`);
});
