// The seat rush, driven by k6 (a compiled load generator) instead of Locust.
//
// Same journey as bench/locustfile.py: every student arrives at once and does one pass of
// reserve, order, pay, with pre-made session tokens (bench-token-<i>). Used to find out
// whether Locust itself was the throughput ceiling in the phase 2 runs.
//
// Environment: TARGET (base url), USERS, SCENARIO (spike | retry-storm), WAITING_ROOM (0 | 1).
import http from "k6/http";
import { sleep } from "k6";
import { Counter, Trend } from "k6/metrics";

// 409 (sold out, already holding) is an expected answer, not a failure.
// 429 is the gateway shedding load on purpose; the student waits a moment and tries again.
http.setResponseCallback(http.expectedStatuses(200, 201, 403, 409, 429));

const shed = new Counter("shed_429");
const journeyReserve = new Trend("reserve_journey_ms", true); // first attempt until a real answer

const USERS = Number(__ENV.USERS || 300);
const TARGET = __ENV.TARGET || "http://127.0.0.1:8090";
const STORM = (__ENV.SCENARIO || "spike") === "retry-storm";
const QUEUE = (__ENV.WAITING_ROOM || "0") === "1";
const EVENT_ID = Number(__ENV.EVENT_ID || 1);
const REFRESHES = Number(__ENV.REFRESHES || 0);
const POLL = Number(__ENV.POLL_SECONDS || 2); // a client polls its position every 2 to 4 seconds

export const options = {
  scenarios: {
    rush: {
      executor: "per-vu-iterations",
      vus: USERS,
      iterations: 1,
      maxDuration: __ENV.MAX_DURATION || "120s",
    },
  },
  summaryTrendStats: ["avg", "med", "p(95)", "p(99)", "max"],
  thresholds: {
    // Declaring a threshold on each tag makes k6 report that request type on its own.
    "http_req_duration{name:reserve}": ["max>=0"],
    "http_req_duration{name:refresh}": ["max>=0"],
    "http_reqs{name:refresh}": ["count>=0"],
    "http_reqs{name:reserve}": ["count>=0"],
    "http_reqs{name:order}": ["count>=0"],
    "http_reqs{name:pay}": ["count>=0"],
    "http_reqs{name:queue_join}": ["count>=0"],
    "http_reqs{name:queue_wait}": ["count>=0"],
    "http_req_duration{name:order}": ["max>=0"],
    "http_req_duration{name:pay}": ["max>=0"],
    "http_req_duration{name:queue_join}": ["max>=0"],
    "http_req_duration{name:queue_wait}": ["max>=0"],
  },
};

function call(method, path, name, key) {
  const headers = { Authorization: `Bearer bench-token-${__VU - 1}` };
  if (key) headers["Idempotency-Key"] = key;
  return http.request(method, `${TARGET}${path}`, null, { headers, tags: { name } });
}

function retrying(method, path, name, key, track) {
  const t0 = Date.now();
  let r;
  for (let attempt = 0; attempt < 60; attempt++) {
    r = call(method, path, name, key);
    if (r.status !== 429) break;
    shed.add(1);
    sleep(0.3 + Math.random() * 0.7);
  }
  if (track) journeyReserve.add(Date.now() - t0);
  return r;
}

// With a storm, send the same write twice at the same instant.
function write(method, path, name, key) {
  if (!STORM) return retrying(method, path, name, key, name === "reserve");
  const headers = { Authorization: `Bearer bench-token-${__VU - 1}` };
  if (key) headers["Idempotency-Key"] = key;
  const req = { method, url: `${TARGET}${path}`, body: null, params: { headers, tags: { name } } };
  const [a] = http.batch([req, req]);
  return a;
}

export default function () {
  // Students refresh the page while they wait for the sale to open; those reads are what a cache absorbs.
  for (let i = 0; i < REFRESHES; i++) {
    http.get(`${TARGET}/api/events/${EVENT_ID}`, { tags: { name: "refresh" } });
    sleep(0.2 + Math.random() * 0.4);
  }
  if (QUEUE) {
    // Join the waiting room and poll until admitted.
    retrying("POST", "/api/queue/join", "queue_join", `q-${__VU}`, false);
    for (let i = 0; i < 400; i++) {
      const s = retrying("GET", "/api/queue/status", "queue_wait", null, false);
      if (s.status === 200 && s.json("admitted") === true) break;
      // The server says when to ask again; a little jitter keeps the polls from lining up.
      const wait = s.status === 200 ? s.json("poll_after_seconds") || POLL : POLL;
      sleep(wait * (0.8 + Math.random() * 0.4));
    }
  }
  const r = write("POST", `/api/events/${EVENT_ID}/reserve`, "reserve", `r-${__VU}`);
  if (r.status !== 201) return;
  const o = write("POST", "/api/orders", "order", `o-${__VU}`);
  if (o.status !== 201) return;
  write("POST", `/api/orders/${o.json("id")}/pay`, "pay");
}
