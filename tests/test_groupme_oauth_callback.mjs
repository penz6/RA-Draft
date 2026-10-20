import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';

const source = readFileSync(new URL('../static/groupme_oauth_callback.js', import.meta.url), 'utf8');
function callback(url, hasForm = true) {
  const calls = [];
  const form = { elements: { access_token: {}, state: {} }, requestSubmit() {
    calls.push({ type: 'post', token: this.elements.access_token.value, state: this.elements.state.value });
  } };
  runInNewContext(source, {
    URLSearchParams,
    window: { location: new URL(url), history: { replaceState(_state, _title, path) { calls.push({ type: 'scrub', path }); } } },
    document: { getElementById() { return hasForm ? form : null; } },
  });
  assert.equal(calls[0].type, 'scrub');
  assert.equal(calls[0].path, '/groupme/callback');
  return calls;
}
assert.deepEqual(callback('https://ci.local/groupme/callback?access_token=query-secret&state=nonce')[1],
  { type: 'post', token: 'query-secret', state: 'nonce' });
assert.deepEqual(callback('https://ci.local/groupme/callback#access_token=fragment-secret&state=nonce')[1],
  { type: 'post', token: 'fragment-secret', state: 'nonce' });
assert.deepEqual(callback('https://ci.local/groupme/callback?error=access_denied')[1],
  { type: 'post', token: '', state: '' });
assert.equal(callback('https://ci.local/groupme/callback?access_token=secret', false).length, 1);
console.log('Passed 4 GroupMe callback scenarios.');
