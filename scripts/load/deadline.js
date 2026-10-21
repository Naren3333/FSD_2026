// submit assessments while students continue reading classroom data.
import http from 'k6/http';
import exec from 'k6/execution';
import { SharedArray } from 'k6/data';
import { Counter, Rate, Trend } from 'k6/metrics';

const BASE = __ENV.BASE_URL;
const RUN = JSON.parse(open('/work/run.json'));
const TOKENS = new SharedArray('tokens', () => RUN.tokens);
const BROWSE_RATE = Number(__ENV.BROWSE_RATE || 600);
const BROWSE_SECONDS = Number(__ENV.BROWSE_SECONDS || 30);

const submitTime = new Trend('submit_duration', true);
const submitOk = new Rate('submit_ok');
const browseTime = new Trend('browse_duration', true);
const browseOk = new Rate('browse_ok');
const rateLimited = new Counter('rate_limited');
const serverErrors = new Counter('server_errors');

export const options = {
  discardResponseBodies: true,
  scenarios: {
    // each student submits once, all starting together.
    submit: { executor: 'per-vu-iterations', vus: RUN.students, iterations: 1, maxDuration: '2m', exec: 'submit' },
    // reads arrive at a steady rate whatever the response times, so a slower system shows as latency and
    // dropped iterations rather than as fewer requests.
    browse: {
      executor: 'constant-arrival-rate',
      rate: BROWSE_RATE,
      timeUnit: '1s',
      duration: `${BROWSE_SECONDS}s`,
      preAllocatedVUs: 50,
      maxVUs: 400,
      exec: 'browse',
    },
  },
};

function auth(token) {
  return { headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' } };
}

function record(res, ok, time, okRate) {
  time.add(res.timings.duration);
  okRate.add(ok);
  if (res.status === 429) rateLimited.add(1);
  if (res.status >= 500 || res.status === 0) serverErrors.add(1);
}

export function submit() {
  // one iteration per vu, so the scenario's iteration number names each student once (vu ids are shared
  // with the browse scenario, so they would not).
  const token = TOKENS[exec.scenario.iterationInTest];
  const params = { ...auth(token), responseType: 'text' };
  const res = http.post(`${BASE}${RUN.submit_path}`, JSON.stringify({ answers: RUN.answers }), params);
  record(res, res.status === 201, submitTime, submitOk);
  if (res.status !== 201) console.error(`submit failed: ${res.status} ${(res.body || res.error).slice(0, 200)}`);
}

export function browse() {
  const n = exec.scenario.iterationInTest;
  const token = TOKENS[n % TOKENS.length];
  const path = RUN.read_paths[n % RUN.read_paths.length];
  const res = http.get(`${BASE}${path}`, auth(token));
  record(res, res.status === 200 || res.status === 404, browseTime, browseOk);
}

function trend(name, data) {
  const m = data.metrics[name];
  return m ? { p50: m.values.med, p95: m.values['p(95)'], max: m.values.max } : null;
}

function count(name, data) {
  const m = data.metrics[name];
  return m ? m.values.count : 0;
}

function rate(name, data) {
  const m = data.metrics[name];
  return m ? { passes: m.values.passes, fails: m.values.fails } : { passes: 0, fails: 0 };
}

export function handleSummary(data) {
  const summary = {
    submit: { ...rate('submit_ok', data), ms: trend('submit_duration', data) },
    browse: { ...rate('browse_ok', data), ms: trend('browse_duration', data), seconds: BROWSE_SECONDS },
    dropped_iterations: count('dropped_iterations', data),
    rate_limited: count('rate_limited', data),
    server_errors: count('server_errors', data),
  };
  return { stdout: `${JSON.stringify(summary)}\n` };
}
