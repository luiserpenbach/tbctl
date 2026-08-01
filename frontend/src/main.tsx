import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "uplot/dist/uPlot.min.css";
import "react-grid-layout/css/styles.css";
import "react-resizable/css/styles.css";
import "./theme.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
