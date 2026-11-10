import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import BotInfo, { isBotPage } from "./pages/BotInfo";
import "./styles.css";

// /bot is the public information page for website owners: it renders before (and without) the
// login gate and never touches the authenticated API.
createRoot(document.getElementById("root")!).render(<StrictMode>{isBotPage() ? <BotInfo /> : <App />}</StrictMode>);
