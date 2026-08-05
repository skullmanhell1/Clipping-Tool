import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App.jsx";
import AuthGate from "./components/AuthGate.jsx";
import "./index.css";

// Mount the React application.
//
// U12: AuthGate decides whether to render the app or a sign-in form. On a single-tenant
// install (the default) it renders the app immediately, so this wrapper costs one
// unauthenticated request and changes nothing else.
ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <AuthGate>{(auth) => <App auth={auth} />}</AuthGate>
  </React.StrictMode>
);
