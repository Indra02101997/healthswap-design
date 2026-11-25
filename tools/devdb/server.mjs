// HealthSwap local database: PGlite (PostgreSQL 18 compiled to WASM) exposed over the Postgres wire protocol so the
// Python services connect with plain psycopg (libpq). Development and tests only; production uses managed PostgreSQL 16+.
//
//   node server.mjs [--port 55432] [--host 127.0.0.1] [--data ../../.data/pglite | --memory] [--reset] [--schema path]
//
// The reference schema (db/schema.sql) is applied on first start. PGlite runs a single backend session; the socket
// server multiplexes client connections and serializes transactions, so applications must not rely on session state
// (use SET LOCAL / set_config(..., true) inside transactions, as the HealthSwap services do).
import { PGlite } from '@electric-sql/pglite';
import { vector } from '@electric-sql/pglite-pgvector';
import { pg_trgm } from '@electric-sql/pglite/contrib/pg_trgm';
import { pgcrypto } from '@electric-sql/pglite/contrib/pgcrypto';
import { PGLiteSocketServer } from '@electric-sql/pglite-socket';
import { existsSync, mkdirSync, readFileSync, rmSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, '..', '..');

function arg(name, fallback) {
  const i = process.argv.indexOf(`--${name}`);
  if (i === -1) return fallback;
  const v = process.argv[i + 1];
  return v === undefined || v.startsWith('--') ? true : v;
}

const port = Number(arg('port', process.env.HS_DEVDB_PORT ?? '55432'));
const host = String(arg('host', '127.0.0.1'));
const memory = arg('memory', false) === true;
const dataDir = memory ? null : resolve(String(arg('data', resolve(repoRoot, '.data', 'pglite'))));
const schemaPath = resolve(String(arg('schema', resolve(repoRoot, 'db', 'schema.sql'))));

if (host !== '127.0.0.1' && host !== 'localhost') {
  console.error('Refusing to bind the development database to a non-loopback address.');
  process.exit(2);
}

if (dataDir && arg('reset', false) === true && existsSync(dataDir)) {
  rmSync(dataDir, { recursive: true, force: true });
  console.log(`devdb: wiped ${dataDir}`);
}
if (dataDir) mkdirSync(dataDir, { recursive: true });

const db = await PGlite.create({
  dataDir: dataDir ?? undefined,
  extensions: { vector, pg_trgm, pgcrypto },
});

const applied = await db.query(`select to_regclass('kb.product') is not null as ok`);
if (!applied.rows[0].ok) {
  const t0 = Date.now();
  await db.exec(readFileSync(schemaPath, 'utf8'));
  console.log(`devdb: applied ${schemaPath} in ${Date.now() - t0} ms`);
} else {
  console.log('devdb: schema already present');
}

const server = new PGLiteSocketServer({ db, port, host, maxConnections: 64 });
await server.start();
const url = `postgresql://postgres:postgres@${host}:${port}/postgres`;
console.log(`DEVDB READY ${url} (${dataDir ?? 'in-memory'})`);

async function shutdown(signal) {
  console.log(`devdb: ${signal}, shutting down`);
  try {
    await server.stop();
    await db.close();
  } finally {
    process.exit(0);
  }
}
process.on('SIGINT', () => shutdown('SIGINT'));
process.on('SIGTERM', () => shutdown('SIGTERM'));
// Parent processes (tests) close stdin to request shutdown.
process.stdin.on('end', () => shutdown('stdin closed'));
process.stdin.resume();
