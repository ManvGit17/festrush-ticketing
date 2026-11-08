// Read-only ceiling: how many GET /api/events/1 per second can the stack serve?
// Used to tell apart "the load generator is the limit" from "the application is the limit".
import http from "k6/http";
import { check } from "k6";

export const options = {
  vus: Number(__ENV.VUS || 200),
  duration: __ENV.DURATION || "15s",
};

export default function () {
  const r = http.get(`${__ENV.TARGET || "http://app:8080"}/api/events/1`);
  check(r, { "status is 200": (x) => x.status === 200 });
}
