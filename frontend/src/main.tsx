import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "@fontsource-variable/geist";
import "@fontsource-variable/geist-mono";
import "uplot/dist/uPlot.min.css";
import "react-grid-layout/css/styles.css";
import "react-resizable/css/styles.css";
import "./theme.css";
import "./theme-pref"; // applies the saved light / dark / system choice

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
