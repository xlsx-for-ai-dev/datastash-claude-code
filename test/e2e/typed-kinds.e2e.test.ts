/**
 * XLI-388 end to end, plugin 1.0.10 against x-live #493's real server (run by test/test_typed_kinds_server.py, which copies
 * this file into an x-live checkout's test/cases and points DATASTASH_PLUGIN_DIR here). On PGlite with every migration,
 * through the real doors: the person's real status reply goes to the plugin's own code (test/e2e_typed_kinds_driver.py:
 * the local copy, the numbered list, the person's typed prompt, the transcript checker, the proof); the request the plugin
 * makes is then sent through the door as it is, with the bearer the plugin used (the datastash login's token).
 * A hand-off answer, a stuck (circle) answer and an undo of a default are click-free with the plugin's proof; the same
 * proof again, a proof moved to another ask or kind, and an edited answer go to /confirm. Every value is made up.
 */
import { describe, it, expect, beforeAll, afterAll, beforeEach } from 'vitest';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import Fastify, { type FastifyInstance } from 'fastify';
import type pg from 'pg';

process.env['API_KEY_HASH_PEPPER'] ??= 'test-pepper-not-a-real-secret';
process.env['LEDGER_FIELD_ENC_KEY'] ??= '44'.repeat(32);
process.env['LEDGER_ACCOUNT_CALLS_PER_MIN'] = '0';

import { startLedgerPgliteMigrated, pgliteAsPool, type PGliteLike } from '../helpers/pglite-ledger.js';
import { bearerFor, newAccount } from '../helpers/door-auth.js';
import { personOpensKey } from '../helpers/pickup-binding.js';
import { callLedgerTool, LEDGER_MANIFEST } from '../../src/mcp/manifest.js';
import { ledgerDoorRoute } from '../../src/mcp/door.js';
import { agentRestRoutes, AGENT_PICKUP_PATH, AGENT_REST_HANDOFF_PATH, AGENT_REST_STATUS_DEFAULT_PATH, AGENT_REST_STATUS_PATH, PERSON_REST_STATUS_ANSWER_PATH, PERSON_REST_STATUS_DEFAULT_UNDO_PATH, PERSON_REST_STATUS_PATH } from '../../src/agents/rest.js';
import { AGENT_KEY_TOOL_NAME } from '../../src/agents/key-tools.js';
import { raiseStalledHandoffs } from '../../src/agents/handoffs.js';
import { sweepStuck } from '../../src/agents/stuck.js';
import { DEFAULT_MIN_WAIT_MS } from '../../src/agents/defaults.js';
import { deviceLoginRestRoutes, startDeviceLogin, answerDeviceCode, DEVICE_TOKEN_PATH } from '../../src/agents/device-grant.js';
import { hashApiKey } from '../../src/platform/lib/api-key-hash.js';
import { setRedis } from '../../src/platform/lib/redis.js';
import { setLedgerPool } from '../../src/db/pool.js';

const PLUGIN = process.env['DATASTASH_PLUGIN_DIR'] ?? '';
let db: PGliteLike;
let pool: pg.Pool;
let door: FastifyInstance;
// eslint-disable-next-line @typescript-eslint/no-explicit-any
let redis: any;

beforeAll(async () => {
  db = await startLedgerPgliteMigrated();
  pool = pgliteAsPool(db);
  setLedgerPool(pool);
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const RedisMock = (await import('ioredis-mock')).default as any;
  redis = new RedisMock();
  setRedis(redis);
  door = Fastify();
  await door.register(ledgerDoorRoute, { manifest: LEDGER_MANIFEST });
  await door.register(agentRestRoutes, { callTool: (n, a, c) => callLedgerTool(n, a, c) });
  await door.register(deviceLoginRestRoutes, { pool: () => pool });
  await door.ready();
}, 60_000);

beforeEach(async () => {
  await redis.flushall();
});

afterAll(async () => {
  await door?.close();
  await db?.close?.().catch(() => undefined);
});

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Body = Record<string, any>;
const HOME = '127.0.0.1';
const parse = (t: string): Body => {
  try {
    return JSON.parse(t) as Body;
  } catch {
    return {};
  }
};
async function rest(method: 'POST' | 'GET', url: string, auth: string | null, payload?: Body): Promise<{ status: number; body: Body }> {
  const res = await door.inject({ method, url, remoteAddress: HOME, headers: auth === null ? {} : { authorization: auth }, ...(payload ? { payload } : {}) });
  return { status: res.statusCode, body: parse(res.body) };
}
async function mcp(auth: string, name: string, args: Body): Promise<Body> {
  const res = await door.inject({ method: 'POST', url: '/mcp', remoteAddress: HOME, headers: { authorization: auth }, payload: { jsonrpc: '2.0', id: 1, method: 'tools/call', params: { name, arguments: args } } });
  const out = res.json() as { result?: { content: Array<{ text: string }> } };
  return parse(out.result?.content?.[0]?.text ?? '');
}
const bearer = (k: string) => `Bearer ${k}`;
const held = (b: Body) => JSON.stringify(b).includes('person_approval_needed');
/** Not done click-free: held for the site yes, or refused. */
const notClickFree = (r: { status: number; body: Body }) => held(r.body) || r.status >= 400;

/** An account with an agent workbook and agents Alpha, Beta and Gamma (keys opened), and a datastash login of the person. */
async function world() {
  const acct = await newAccount(db);
  await db.query(`UPDATE x.accounts SET standing = 'paying' WHERE account_id = $1`, [acct]);
  const person = await bearerFor(db, acct);
  const agents = await bearerFor(db, acct);
  const wb = (await mcp(person, 'ledger_agent_workbook', { action: 'create', name: `Agents ${Math.random().toString(36).slice(2, 7)}` }))['workbook_id'] as string;
  expect(wb).toBeTruthy();
  const keys: Record<string, string> = {};
  for (const name of ['Alpha', 'Beta', 'Gamma']) {
    await mcp(agents, 'ledger_agent_register', { name });
    const issued = await mcp(person, AGENT_KEY_TOOL_NAME, { name });
    const res = await door.inject({ method: 'POST', url: AGENT_PICKUP_PATH, remoteAddress: HOME, headers: { 'content-type': 'application/x-www-form-urlencoded' }, payload: `code=${encodeURIComponent(issued['pickup_code'] as string)}` });
    expect(res.statusCode).toBe(200);
    await personOpensKey(db, res.body.trim());
    keys[name] = bearer(res.body.trim());
  }
  const s = await startDeviceLogin(pool, 'Test laptop', 'test', HOME, null);
  expect(await answerDeviceCode(pool, acct, s.userCode, true)).toBe(true);
  const t = await rest('POST', DEVICE_TOKEN_PATH, null, { device_code: s.deviceCode });
  expect(t.status, JSON.stringify(t.body)).toBe(200);
  const g = (await db.query(`SELECT grant_id::text AS id FROM x.device_codes WHERE device_code_hash = $1`, [hashApiKey(s.deviceCode)])).rows[0] as { id: string };
  await db.query(`UPDATE x.device_grants SET reach_open = true WHERE grant_id = $1::uuid`, [g.id]);
  return { acct, wb, person, alpha: keys['Alpha']!, beta: keys['Beta']!, token: t.body['access_token'] as string, answerKey: t.body['answer_key'] as string, grant: g.id };
}
type World = Awaited<ReturnType<typeof world>>;

async function handoffAsk(w: World): Promise<string> {
  const r = await rest('POST', AGENT_REST_HANDOFF_PATH, w.alpha, { workbook_id: w.wb, to_agent: 'Beta', task: 'Review the open card', project: 'api' });
  expect(r.status, JSON.stringify(r.body)).toBe(200);
  await db.query(`UPDATE x.agent_handoffs SET stall_due_at = now() - interval '1 second' WHERE handoff_id = $1::uuid`, [r.body['handoff_id']]);
  await raiseStalledHandoffs(pool);
  const st = (await rest('GET', `${PERSON_REST_STATUS_PATH}?workbook_id=${w.wb}`, w.person)).body;
  return (st['handoffs'] as Body[]).find((h) => h['ask'])!['ask']['instance'] as string;
}
async function circleAsk(w: World): Promise<string> {
  const set = (auth: string, who: string) => rest('POST', AGENT_REST_STATUS_PATH, auth, { workbook_id: w.wb, project: 'web', doing: 'Working on it.', waiting_on: [{ who, what: 'the token format' }] });
  expect((await set(w.alpha, 'Beta')).status).toBe(200);
  expect((await set(w.beta, 'Alpha')).status).toBe(200);
  await sweepStuck(pool);
  const st = (await rest('GET', `${PERSON_REST_STATUS_PATH}?workbook_id=${w.wb}`, w.person)).body;
  return (st['stuck'] as Body[])[0]!['instance'] as string;
}
async function defaultTaken(w: World): Promise<string> {
  const ask = { id: 'a1', text: 'One-day or seven-day sessions?', options: ['one day', 'seven days'], default: 'one day', by_when: '2020-01-01T00:00:00Z' };
  expect((await rest('POST', AGENT_REST_STATUS_PATH, w.alpha, { workbook_id: w.wb, project: 'api', doing: 'Working on it.', asks: [ask] })).status).toBe(200);
  const r = await rest('GET', `${AGENT_REST_STATUS_PATH}?workbook_id=${w.wb}`, w.alpha);
  const inst = ((r.body['statuses'] as Body[]).find((s) => s['agent'] === 'Alpha' && s['project'] === 'api')!['asks'] as Body[])[0]!['instance'] as string;
  await db.query(`UPDATE x.agent_ask_instances SET opened_at = now() - ($2::double precision * interval '1 millisecond') WHERE instance_id = $1::uuid`, [inst, DEFAULT_MIN_WAIT_MS + 60_000]);
  expect((await rest('POST', AGENT_REST_STATUS_DEFAULT_PATH, w.alpha, { workbook_id: w.wb, project: 'api', ask_id: 'a1' })).body).toMatchObject({ taken: true });
  return inst;
}

/** The plugin, from the person's real status reply to the requests it makes for these typed prompts. */
function plugin(w: World, typed: Array<{ instance: string; say: string }>, status: Body): Array<Body> {
  const dir = mkdtempSync(join(tmpdir(), 'ds-e2e-'));
  const input = join(dir, 'in.json');
  const output = join(dir, 'out.json');
  writeFileSync(input, JSON.stringify({ workbook_id: w.wb, status, answer_key: w.answerKey, grant: w.grant, token: w.token, typed }), { mode: 0o600 });
  execFileSync('python3', ['-I', join(PLUGIN, 'test', 'e2e_typed_kinds_driver.py'), input, output], { stdio: ['ignore', 'ignore', 'inherit'], timeout: 120_000 });
  return (JSON.parse(readFileSync(output, 'utf8')) as Body)['requests'] as Body[];
}
const send = (r: Body, body: Body = r['body']) => rest(r['method'], r['path'], bearer(r['bearer']), body);

describe('XLI-388: the plugin numbers, proves and signs each kind; the real server takes it click-free', () => {
  it('hand-off, stuck and undo-default from the plugin pass; replays and moved proofs go to /confirm', async () => {
    expect(PLUGIN, 'DATASTASH_PLUGIN_DIR').not.toBe('');
    const w = await world();
    const hand = await handoffAsk(w);
    const circle = await circleAsk(w);
    const dflt = await defaultTaken(w);
    const status = (await rest('GET', `${PERSON_REST_STATUS_PATH}?workbook_id=${w.wb}`, w.person)).body;
    const reqs = plugin(w, [{ instance: hand, say: '{n}: gamma' }, { instance: circle, say: '{n}: Beta' }, { instance: dflt, say: 'undo {n}' }], status);
    expect(reqs.map((r) => [r['path'], r['instance']])).toEqual([[PERSON_REST_STATUS_ANSWER_PATH, hand], [PERSON_REST_STATUS_ANSWER_PATH, circle], [PERSON_REST_STATUS_DEFAULT_UNDO_PATH, dflt]]);
    const [rh, rc, ru] = reqs as [Body, Body, Body];
    expect(rh['bearer']).toBe(w.token);
    expect(rh['body']['answer']).toBe('Gamma');
    expect(Object.keys(ru['body']).sort()).toEqual(['instance', 'typed_proof', 'workbook_id']);

    // A proof moved to another kind or ask, or an edited answer, is never click-free (and never spends the real one).
    const moved = [
      await rest('POST', PERSON_REST_STATUS_ANSWER_PATH, bearer(w.token), { workbook_id: w.wb, instance: dflt, answer: 'undo_default', typed_proof: ru['body']['typed_proof'] }),
      await rest('POST', PERSON_REST_STATUS_ANSWER_PATH, bearer(w.token), { workbook_id: w.wb, instance: hand, answer: 'Gamma', typed_proof: ru['body']['typed_proof'] }),
      await send(rh, { ...rh['body'], answer: 'Beta' }),
      await send(rh, { ...rh['body'], instance: circle, answer: 'Beta' }),
      await rest('POST', PERSON_REST_STATUS_DEFAULT_UNDO_PATH, bearer(w.token), { workbook_id: w.wb, instance: dflt, typed_proof: rh['body']['typed_proof'] }),
    ];
    for (const m of moved) expect(notClickFree(m), JSON.stringify(m.body)).toBe(true);
    expect(((await db.query(`SELECT undone_at IS NULL AS n FROM x.agent_defaults_taken WHERE instance_id = $1::uuid`, [dflt])).rows[0] as Body)['n']).toBe(true);

    const h = await send(rh);
    expect(h.status, JSON.stringify(h.body)).toBe(200);
    expect(h.body).toMatchObject({ changed: true, reassigned_to: 'Gamma' });
    const c = await send(rc);
    expect(c.status, JSON.stringify(c.body)).toBe(200);
    expect(held(c.body)).toBe(false);
    expect(((await db.query(`SELECT answered_at IS NOT NULL AS a FROM x.agent_stuck_asks WHERE ask_id = $1::uuid`, [circle])).rows[0] as Body)['a']).toBe(true);
    const u = await send(ru);
    expect(u.status, JSON.stringify(u.body)).toBe(200);
    expect(u.body).toMatchObject({ undone: true, how: 'click_free' });

    // The same plugin request again: its prompt id is spent.
    const w2 = await world();
    const hand2 = await handoffAsk(w2);
    const st2 = (await rest('GET', `${PERSON_REST_STATUS_PATH}?workbook_id=${w2.wb}`, w2.person)).body;
    const [r2] = plugin(w2, [{ instance: hand2, say: '{n}: Gamma' }], st2) as [Body];
    expect((await send(r2)).status).toBe(200);
    const again = await send(r2);
    expect(notClickFree(again), JSON.stringify(again.body)).toBe(true);
  }, 240_000);

  it('the login token alone (no proof) still goes to /confirm for all three kinds', async () => {
    const w = await world();
    const hand = await handoffAsk(w);
    const circle = await circleAsk(w);
    const dflt = await defaultTaken(w);
    expect(held((await rest('POST', PERSON_REST_STATUS_ANSWER_PATH, bearer(w.token), { workbook_id: w.wb, instance: hand, answer: 'Gamma' })).body)).toBe(true);
    expect(held((await rest('POST', PERSON_REST_STATUS_ANSWER_PATH, bearer(w.token), { workbook_id: w.wb, instance: circle, answer: 'Beta' })).body)).toBe(true);
    expect(held((await rest('POST', PERSON_REST_STATUS_DEFAULT_UNDO_PATH, bearer(w.token), { workbook_id: w.wb, instance: dflt })).body)).toBe(true);
  }, 120_000);
});
