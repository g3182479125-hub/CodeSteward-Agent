import { useState } from "react";

export default function App() {
  const [submitted, setSubmitted] = useState(false);

  function handleSubmit(event) {
    // G-02 defect: the browser performs a default form navigation.
    setSubmitted(true);
  }

  return (
    <form onSubmit={handleSubmit}>
      <label>
        Email
        <input name="email" type="email" />
      </label>
      <button type="submit">Save</button>
      {submitted ? <p role="status">Saved</p> : null}
    </form>
  );
}
