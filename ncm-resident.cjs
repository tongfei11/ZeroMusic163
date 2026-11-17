// Keep the command tree of the pinned official CLI alive between UI requests.
const fs = require('node:fs');
const Module = require('node:module');
const path = require('node:path');
const readline = require('node:readline');

const entry = fs.realpathSync(process.argv[2]);
const source = fs.readFileSync(entry, 'utf8');
const marker = 'let c=process[';
const offset = source.lastIndexOf(marker);
if (offset < 0 || source.indexOf(marker) !== offset) process.exit(1);

const write = process.stdout.write.bind(process.stdout);
let output = '';
let errors = '';
process.stdout.write = (chunk, encoding, callback) => {
  output += chunk.toString();
  if (typeof encoding === 'function') encoding();
  if (callback) callback();
  return true;
};
process.stderr.write = (chunk, encoding, callback) => {
  errors += chunk.toString();
  if (typeof encoding === 'function') encoding();
  if (callback) callback();
  return true;
};
class CommandExit extends Error {
  constructor(code) { super('command exit'); this.code = code; }
}
process.exit = code => { throw new CommandExit(code || 0); };
process.argv = [process.execPath, entry];
const instance = new Module(entry, module);
instance.filename = entry;
instance.paths = Module._nodeModulePaths(path.dirname(entry));
instance._compile(source.slice(0, offset) +
  'module.exports={program:t, initialize:a, runner:async()=>{gn();return await fn.createCommandRunner();}};}', entry);

const {program, initialize, runner} = instance.exports;
let defaults;
function remember(command) {
  return [command, {...command._optionValues}, {...command._optionValueSources}, command.commands.map(remember)];
}
function reset([command, values, sources, children]) {
  command._optionValues = {...values};
  command._optionValueSources = {...sources};
  command.args = [];
  command.processedArgs = [];
  children.forEach(reset);
}
async function run(request) {
  output = errors = '';
  let code = 0;
  try {
    if (!defaults) {
      await initialize();
      defaults = remember(program);
    }
    reset(defaults);
    if (request.media) {
      const api = await runner();
      const lyric = api.manifests.song.methods.find(method => method.name === 'lyric');
      const method = {...lyric, path: '/openapi/music/basic/song/detail/get/v2',
        parameters: [...lyric.parameters,
          {name: 'withUrl', in: 'query', type: 'boolean'},
          {name: 'bitrate', in: 'query', type: 'number'},
          {name: 'trialScene', in: 'query', type: 'string'}], fixed_params: null};
      const response = await api.executor.execute(method,
        {songId: request.media, withUrl: true, bitrate: 128, trialScene: 'cli'});
      output = JSON.stringify(response);
    } else {
      process.argv = [process.execPath, entry, ...request.args];
      await program.parseAsync(process.argv);
    }
  } catch (error) {
    code = error instanceof CommandExit ? error.code : 1;
    if (!(error instanceof CommandExit)) errors += error.message || 'request failed';
  }
  write(JSON.stringify({output, errors, code}) + '\n');
}
let pending = Promise.resolve();
const input = readline.createInterface({input: process.stdin});
input.on('line', line => {
  pending = pending.then(() => run(JSON.parse(line))).catch(() => write('{"code":1,"output":"","errors":""}\n'));
});
input.on('close', () => pending.finally(() => {
  // The official command tree may retain timers. Closing stdin ends the service.
  require('node:process').reallyExit(0);
}));
