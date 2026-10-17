import {readFile, mkdir, writeFile} from 'node:fs/promises';

// This is the only value read from the build environment. It is a PUBLIC URL.
const value = process.env.RTL_AGENT_API_BASE_URL?.trim();
let backend;
try {
  const url = new URL(value);
  if (url.protocol !== 'https:' || url.username || url.password ||
      (url.pathname !== '/' && url.pathname !== '') || url.search || url.hash ||
      url.port || !/^[a-z0-9.-]+$/i.test(url.hostname)) throw new Error();
  backend = url.origin;
} catch {
  throw new Error('Set RTL_AGENT_API_BASE_URL to your backend HTTPS origin, without a path.');
}
const page = await readFile(new URL('../web/index.html', import.meta.url), 'utf8');
const marker = '<script id="rtl-agent-public-config" type="application/json">{"apiBaseUrl":""}</script>';
if (!page.includes(marker)) throw new Error('The frontend configuration marker is missing.');
const output = page.replace(marker, marker.replace('{"apiBaseUrl":""}',
  JSON.stringify({apiBaseUrl: backend}).replaceAll('<', '\\u003c')));
const destination = new URL('../dist/', import.meta.url);
await mkdir(destination, {recursive: true});
await writeFile(new URL('index.html', destination), output);
console.log('Built the static frontend. Server secrets are not part of this build.');
