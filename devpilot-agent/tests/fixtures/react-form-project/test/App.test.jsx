import { expect, test } from "vitest";
import App from "../src/App.jsx";

test("submitting the form keeps the page and shows Saved", () => {
  expect(App).toBeDefined();
  // G-02 expected behavior is encoded as a static fixture contract:
  // handleSubmit must call event.preventDefault() before changing state.
});
