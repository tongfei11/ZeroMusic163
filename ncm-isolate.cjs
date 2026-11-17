// Keep the official CLI and its detached workers in this app's profile.
const fs = require('node:fs');
const os = require('node:os');
const profile = process.env.ZEROMUSIC_NCM_PROFILE;
const runtime = process.env.ZEROMUSIC_NCM_RUNTIME;
if (profile && runtime) {
  os.homedir = () => profile;
  os.tmpdir = () => runtime;
  require('node:module').syncBuiltinESMExports();
}
if (process.env.ZEROMUSIC_NCM_CONFIG_INPUT === '1') {
  delete process.env.ZEROMUSIC_NCM_CONFIG_INPUT;
  process.argv.push(fs.readFileSync(0, 'utf8'));
}

// On the pinned CLI, the player worker requests the signed media URL after
// authentication. Hand it to the device's native mpv before the worker opens
// its own player. All ordinary CLI commands keep their original behavior.
const mediaFile = process.env.ZEROMUSIC_NCM_MEDIA_FILE;
if (mediaFile && process.argv[2] === 'play') {
  const timer = global.setTimeout;
  const fetch = global.fetch;
  const exit = (info) => {
    fs.writeFileSync(mediaFile, JSON.stringify(info), {mode: 0o600});
    process.exit(0);
  };
  global.setTimeout = function (callback, delay, ...args) {
    if (delay === 3000 && new Error().stack.includes('.sendCommand')) delay = 20000;
    return timer.call(this, callback, delay, ...args);
  };
  global.fetch = async function (input, ...args) {
    let target;
    try {
      target = new URL(typeof input === 'string' ? input : input.url || input.href);
    } catch {
      return fetch.call(this, input, ...args);
    }
    if (target.hostname !== 'openncm.music.163.com' ||
        target.pathname !== '/openapi/music/basic/song/detail/get/v2') {
      return fetch.call(this, input, ...args);
    }
    let response;
    try {
      response = await fetch.call(this, input, ...args);
    } catch {
      return exit({network: true});
    }
    const data = await response.json().catch(() => null);
    if (response.status >= 500) return exit({network: true});
    return exit({url: data?.code === 200 ? data.data?.playUrl : undefined});
  };
}
