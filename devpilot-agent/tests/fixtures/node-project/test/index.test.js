import test from "node:test";
import assert from "node:assert/strict";
import { main, formatGreeting } from "../src/index.js";

test("main returns ok", () => {
  assert.equal(main(), "ok");
});

test("formatGreeting returns greeting with name", () => {
  assert.equal(formatGreeting("World"), "Hello, World!");
});
