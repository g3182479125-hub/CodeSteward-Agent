import test from "node:test";
import assert from "node:assert/strict";
import { main } from "../src/index.js";

test("main returns ok", () => {
  assert.equal(main(), "ok");
});
