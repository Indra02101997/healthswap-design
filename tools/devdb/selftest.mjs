// Self-test for db/schema.sql: applies it to an in-memory PGlite and checks row-level security, role grants and the
// append-only, balanced ledger. Run: npm run selftest
import { PGlite } from '@electric-sql/pglite';
import { vector } from '@electric-sql/pglite-pgvector';
import { pg_trgm } from '@electric-sql/pglite/contrib/pg_trgm';
import { pgcrypto } from '@electric-sql/pglite/contrib/pgcrypto';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const schema = readFileSync(resolve(here, '..', '..', 'db', 'schema.sql'), 'utf8');
const db = await PGlite.create({ extensions: { vector, pg_trgm, pgcrypto } });
await db.exec(schema);

let failures = 0;
const check = (name, ok, detail = '') => {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  ' + detail : ''}`);
  if (!ok) failures++;
};
const expectError = async (name, fn, pattern) => {
  try {
    await fn();
    check(name, false, 'expected an error');
  } catch (e) {
    check(name, pattern.test(e.message), e.message);
  }
};

const a = '11111111-1111-4111-8111-111111111111';
const b = '22222222-2222-4222-8222-222222222222';
await db.exec(`
  insert into tenant.partner (partner_id, display_name, legal_name, geography, markets) values
    ('${a}', 'A', 'A Ltd', 'IN', '{IN}'), ('${b}', 'B', 'B Inc', 'US', '{US}');
  insert into tenant.store (partner_id, store_id, market, timezone) values
    ('${a}', 'S1', 'IN', 'Asia/Kolkata'), ('${b}', 'S1', 'US', 'America/New_York');`);

const asTenant = async (role, partner, sql, params = []) =>
  db.transaction(async (tx) => {
    await tx.query(`set local role ${role}`);
    await tx.query(`select set_config('app.partner_id', $1, true)`, [partner]);
    return tx.query(sql, params);
  });

const stores = await asTenant('hs_app', a, 'select partner_id from tenant.store');
check('RLS: tenant sees only its stores', stores.rows.length === 1 && stores.rows[0].partner_id === a);
const partners = await asTenant('hs_app', a, 'select partner_id from tenant.partner');
check('RLS: tenant sees only its partner row', partners.rows.length === 1);
const none = await db.transaction(async (tx) => {
  await tx.query('set local role hs_app');
  return tx.query('select * from tenant.store');
});
check('RLS: missing tenant setting fails closed', none.rows.length === 0);
await expectError('Grants: hs_app cannot change partner config',
  () => asTenant('hs_app', a, `update tenant.partner set config = '{}' where partner_id = $1`, [a]), /permission denied/);
await expectError('RLS: cross-tenant insert rejected',
  () => asTenant('hs_app', a, `insert into tenant.store (partner_id, store_id, market, timezone) values ($1, 'X', 'US', 'UTC')`, [b]),
  /row-level security/);

// Ledger: balanced RA-1 posting succeeds, unbalanced fails, rows are immutable.
await db.transaction(async (tx) => {
  await tx.query('set local role hs_ledger_writer');
  await tx.query(`select set_config('app.partner_id', $1, true)`, [a]);
  const ar = await tx.query(`insert into billing.account (partner_id, code, currency) values ($1, 'UNBILLED_AR', 'INR') returning account_id`, [a]);
  const rev = await tx.query(`insert into billing.account (partner_id, code, currency) values ($1, 'RECO_FEE_REVENUE', 'INR') returning account_id`, [a]);
  const e = await tx.query(`insert into billing.journal_entry (partner_id, entry_kind, reference_type, reference_id, effective_at, created_by, idempotency_key)
                            values ($1, 'RECO_FEE', 'RECO_CHARGE', 'c1', now(), 'selftest', 'RA1:c1') returning entry_id`, [a]);
  await tx.query(`insert into billing.journal_line (entry_id, line_no, partner_id, account_id, debit) values ($1, 1, $2, $3, 1.00)`, [e.rows[0].entry_id, a, ar.rows[0].account_id]);
  await tx.query(`insert into billing.journal_line (entry_id, line_no, partner_id, account_id, credit) values ($1, 2, $2, $3, 1.00)`, [e.rows[0].entry_id, a, rev.rows[0].account_id]);
});
check('Ledger: balanced RA-1 entry committed', true);
await expectError('Ledger: unbalanced entry rejected at commit', () => db.transaction(async (tx) => {
  await tx.query('set local role hs_ledger_writer');
  await tx.query(`select set_config('app.partner_id', $1, true)`, [a]);
  const acc = await tx.query(`select account_id from billing.account where code = 'UNBILLED_AR'`);
  const e = await tx.query(`insert into billing.journal_entry (partner_id, entry_kind, reference_type, reference_id, effective_at, created_by, idempotency_key)
                            values ($1, 'RECO_FEE', 'RECO_CHARGE', 'c2', now(), 'selftest', 'RA1:c2') returning entry_id`, [a]);
  await tx.query(`insert into billing.journal_line (entry_id, line_no, partner_id, account_id, debit) values ($1, 1, $2, $3, 1.00)`, [e.rows[0].entry_id, a, acc.rows[0].account_id]);
}), /not balanced/);
await expectError('Ledger: journal is append-only', () => db.query(`update billing.journal_line set debit = 2 where line_no = 1`), /append-only/);
const ledgerB = await asTenant('hs_ledger_writer', b, 'select count(*)::int as n from billing.journal_line');
check('RLS: other tenant cannot read ledger lines', ledgerB.rows[0].n === 0);
await expectError('Grants: hs_app cannot post journal entries', () => asTenant('hs_app', a,
  `insert into billing.journal_entry (partner_id, entry_kind, reference_type, reference_id, effective_at, created_by, idempotency_key)
   values ($1, 'RECO_FEE', 'RECO_CHARGE', 'c3', now(), 'x', 'k3')`, [a]), /permission denied/);

// RA-1 charge table constraints.
await expectError('RA-1: duplicate event cannot create a second charge', () => asTenant('hs_app', a, `
  insert into txn.reco_charge (charge_id, partner_id, event_id, event_type, suggestion_id, cart_ref, market, placement,
    alternative_partner_sku, basis, unit_amount, amount, currency, rule_version, state, occurred_at, finalizes_at)
  select gen_random_uuid(), $1, '33333333-3333-4333-8333-333333333333', 'ADD_TO_CART', gen_random_uuid(), 'c', 'IN', 'CART',
         'SKU', 'PER_ADD_EVENT', 1, 1, 'INR', 'RA-1', 'PENDING', now(), now() from generate_series(1, 2)`, [a]), /duplicate key/);

await db.close();
if (failures) {
  console.error(`${failures} check(s) failed`);
  process.exit(1);
}
console.log('schema self-test passed');
