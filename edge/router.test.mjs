// Runs the CloudFront Function source under Node with a fake `cloudfront` module.
// Usage: node --test edge/
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const source = readFileSync(new URL('./traveler-router.js', import.meta.url), 'utf8');

function load(store) {
  const calls = [];
  const cf = {
    kvs: () => ({
      get: async (key) => {
        if (!(key in store)) throw new Error(`Key ${key} not found`);
        return store[key];
      },
    }),
    updateRequestOrigin: (params) => calls.push(params),
  };
  const body = source.replace(/^import cf from 'cloudfront';$/m, '');
  const handler = new Function('cf', `${body}\nreturn handler;`)(cf);
  return { handler, calls };
}

const event = { request: { method: 'GET', uri: '/api/traveler/whereami', headers: {} } };

test('live: routes to the current origin with Lambda OAC signing', async () => {
  const { handler, calls } = load({ status: 'live', current_origin: 'abc.lambda-url.eu-central-1.on.aws' });
  const out = await handler(event);
  assert.equal(out, event.request);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].domainName, 'abc.lambda-url.eu-central-1.on.aws');
  assert.equal(calls[0].originAccessControlConfig.originType, 'lambda');
});

test('resting: answers 503 at the edge without touching an origin', async () => {
  const { handler, calls } = load({ status: 'resting', current_origin: 'abc.lambda-url.eu-central-1.on.aws' });
  const out = await handler(event);
  assert.equal(out.statusCode, 503);
  assert.equal(JSON.parse(out.body.data).status, 'resting');
  assert.equal(calls.length, 0);
});

test('empty store: treated as resting', async () => {
  const { handler, calls } = load({});
  const out = await handler(event);
  assert.equal(out.statusCode, 503);
  assert.equal(calls.length, 0);
});

test('live but no origin key: treated as resting', async () => {
  const { handler } = load({ status: 'live' });
  assert.equal((await handler(event)).statusCode, 503);
});
