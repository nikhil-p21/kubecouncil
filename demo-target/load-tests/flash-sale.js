import http from "k6/http";
import { check, sleep } from "k6";

const target = __ENV.TARGET_URL || "http://gateway";
const phase = __ENV.PHASE || "baseline";
const baselineVus = Number(__ENV.BASELINE_VUS || "5");
const pressureVus = Number(__ENV.PRESSURE_VUS || "40");
const duration = __ENV.DURATION || "45s";

export const options = {
  scenarios: {
    traffic: {
      executor: "constant-vus",
      vus: phase === "pressure" ? pressureVus : baselineVus,
      duration,
    },
  },
  thresholds: {
    http_req_failed: ["rate<0.05"],
    http_req_duration: ["p(95)<2000"],
  },
};

export default function () {
  const response = http.get(target);
  check(response, {
    "request succeeded": (res) => res.status >= 200 && res.status < 500,
    "checkout path returned": (res) => res.body.includes("checkout"),
  });
  sleep(1);
}
