// Runs levain/templates/web/sw_open.js in a vm with a stub page; prints what the page did for each message.
// usage: node _sw_open_rig.js <path to sw_open.js>
const fs = require('fs');
const vm = require('vm');
const handlers = [];
const log = { assigned: [], acks: [], started: false };
const sandbox = {
  URL,
  location: { origin: 'https://box.example', href: 'https://box.example/cockpit', assign: (u) => log.assigned.push(u) },
  navigator: { serviceWorker: { addEventListener: (t, fn) => handlers.push(fn), startMessages: () => { log.started = true; } } },
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), sandbox);
const send = (data) => handlers.forEach((h) => h({ data, ports: [{ postMessage: (m) => log.acks.push({ m, assignedSoFar: log.assigned.length }) }] }));
send({ type: 'levain-open', url: '/sign/9#sign=a&v=1' });
send({ type: 'levain-open', url: 'https://evil.example/x' });
send({ type: 'levain-open', url: 'javascript:alert(1)' });
send({ type: 'levain-open' });
send({ type: 'other', url: '/x' });
console.log(JSON.stringify(log));
