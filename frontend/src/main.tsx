// =============================================================================
// main.tsx: WHERE THE WEBSITE STARTS
// -----------------------------------------------------------------------------
// index.html has an empty <div id="root"></div>. This file tells React: "draw the
// <App /> component inside that div". Everything you see on the site lives under App.
// Vite (the build tool) starts from this file and follows the imports to find the rest.
// =============================================================================
import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./styles.css";          // importing a CSS file applies those styles to the page

// document.getElementById("root")! : the "!" tells TypeScript "trust me, this isn't null".
// StrictMode is a development helper: it runs some checks twice to surface bugs early.
// It has no effect in the production build.
ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
