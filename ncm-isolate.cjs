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
