import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import "./index.css";

function App() {
  return <main>DevPilot Agent · P0 baseline</main>;
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
