import { chmod, mkdir, readFile, rename, writeFile } from 'node:fs/promises';
import { join } from 'node:path';

// Credentials arrive over stdin, never in argv, Compose environment, or logs.
let input = '';
for await (const chunk of process.stdin) input += chunk;
const source = JSON.parse(input.replace(/^\uFEFF/, ''));
const remote = source.gateway?.remote;
if (!remote || typeof remote.url !== 'string') {
  throw new Error('The source configuration must contain gateway.remote.url.');
}
const endpoint = new URL(remote.url);
if (!['ws:', 'wss:'].includes(endpoint.protocol) || endpoint.username || endpoint.password) {
  throw new Error('Use a ws:// or wss:// Gateway URL without embedded credentials.');
}
if (endpoint.search || endpoint.hash) {
  throw new Error('Keep credentials and routing parameters out of the Gateway URL.');
}
if (remote.sshTarget) {
  throw new Error('This container needs a directly reachable Gateway URL; SSH tunnels are not configured.');
}
if (!['token', 'password'].some((key) => typeof remote[key] === 'string' && remote[key].trim())) {
  throw new Error('The source configuration must contain a nonempty remote token or password.');
}

const stateDir = join(process.env.HOME, '.openclaw');
const configPath = join(stateDir, 'openclaw.json');
await mkdir(stateDir, { recursive: true, mode: 0o700 });
await mkdir(join(stateDir, 'workspace'), { recursive: true, mode: 0o700 });
let config = {};
try {
  config = JSON.parse(await readFile(configPath, 'utf8'));
} catch (error) {
  if (error.code !== 'ENOENT') throw error;
}
const connection = { url: remote.url };
for (const key of ['token', 'password', 'tlsFingerprint']) {
  if (typeof remote[key] === 'string' && remote[key].trim()) connection[key] = remote[key];
}
config.gateway = { ...config.gateway, mode: 'remote', remote: connection };
// Merge only connection settings, preserving this client's identity and future
// local extensions. Never import server agents, channels, or model credentials.
const next = `${JSON.stringify(config, null, 2)}\n`;
const tempPath = `${configPath}.tmp`;
await writeFile(tempPath, next, { mode: 0o600 });
await chmod(tempPath, 0o600);
await rename(tempPath, configPath);
await chmod(stateDir, 0o700);
console.log(`[host-client] Remote configuration saved for ${endpoint.origin}.`);
