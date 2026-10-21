// =============================================================================
// App.tsx: THE PAGE FRAME: HEADER, TABS, ADMIN TOKEN BOX, AND THE CURRENT TAB
// -----------------------------------------------------------------------------
// React basics used here:
//   * A COMPONENT is a function that returns JSX (HTML-like markup). <Leaderboard /> is
//     a component defined in pages/Leaderboard.tsx.
//   * STATE: const [tab, setTab] = useState(...) holds a value that, when changed with
//     setTab, makes React re-draw the component with the new value.
//   * EFFECTS: useEffect(() => {...}, [deps]) runs code AFTER drawing, again whenever a
//     value in [deps] changes. Used for things outside React: timers, events, fetching.
//
// Tabs use the URL "hash" (#leaderboard), so a link to a tab works and Back/Forward work.
// =============================================================================
import { useEffect, useState } from "react";
import { adminToken, api, API_BASE } from "./api";
import Submit from "./pages/Submit";
import Leaderboard from "./pages/Leaderboard";
import Ideas from "./pages/Ideas";
import Signals from "./pages/Signals";
import Review from "./pages/Review";
import System from "./pages/System";

// adminOnly tabs are hidden unless the token is verified by the server. Hiding them is
// only a UI convenience: the real protection is that their API endpoints return 401
// without a valid token.
// `as const` makes TypeScript treat the ids as exact strings ("submit"...), not just `string`.
const TABS = [
  { id: "submit", label: "Submit", el: <Submit />, adminOnly: false },
  { id: "leaderboard", label: "Leaderboard", el: <Leaderboard />, adminOnly: false },
  { id: "ideas", label: "Ideas", el: <Ideas />, adminOnly: false },
  { id: "signals", label: "Signals", el: <Signals />, adminOnly: false },
  { id: "review", label: "Review", el: <Review />, adminOnly: true },
  { id: "system", label: "System", el: <System />, adminOnly: true },
] as const;

// Types derived from TABS: TabId = "submit" | "leaderboard" | ... (so a typo is a compile error).
type TabId = (typeof TABS)[number]["id"];
type AdminState = "none" | "checking" | "valid" | "invalid";

// Read the tab from the URL (#ideas -> "ideas"); unknown or empty -> "submit".
function hashTab(): TabId {
  const h = window.location.hash.replace("#", "");
  return (TABS.find((t) => t.id === h)?.id ?? "submit") as TabId;   // ?. and ?? handle "not found"
}

export default function App() {
  const [tab, setTab] = useState<TabId>(hashTab());
  const [token, setToken] = useState(adminToken.get());       // remembered in localStorage
  const [admin, setAdmin] = useState<AdminState>(token ? "checking" : "none");

  // When the URL hash changes (clicking a tab link, Back button), switch tabs.
  // The returned function is the "cleanup": it removes the listener if App ever unmounts.
  // [] = run once, when App first appears.
  useEffect(() => {
    const onHash = () => setTab(hashTab());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  // Verify the token with the server (debounced while typing).
  // "Debounced": wait 400 ms after the last keystroke before calling the API. Each new
  // keystroke cancels the previous timer (the cleanup), so typing a 30-character token
  // makes one request, not 30.
  useEffect(() => {
    if (!token) { setAdmin("none"); return; }
    setAdmin("checking");
    const id = setTimeout(async () => {
      try { await api("/api/admin/check", { admin: true }); setAdmin("valid"); }   // 200 -> valid
      catch { setAdmin("invalid"); }                                               // 401 -> invalid
    }, 400);
    return () => clearTimeout(id);
  }, [token]);

  const isAdmin = admin === "valid";
  const visible = TABS.filter((t) => !t.adminOnly || isAdmin);    // hide admin tabs unless verified
  const current = visible.find((t) => t.id === tab) ?? visible[0];

  // The markup. className = HTML's "class" (styles are in styles.css).
  // {visible.map(...)} turns each tab into a link; `key` helps React track list items.
  // The token input: every keystroke updates state (re-runs the check above) and saves
  // the token to localStorage. <main key={current.id}> makes React start the tab fresh
  // each time you switch (so its data is re-fetched).
  return (
    <div className="shell">
      <header className="top">
        <div className="brand">
          <span className="logo">▍▌▋</span>
          <div>
            <div className="name">Tally</div>
            <div className="tagline">who actually makes good calls?</div>
          </div>
        </div>
        <nav className="tabs">
          {visible.map((t) => (
            <a key={t.id} href={`#${t.id}`} className={current.id === t.id ? "active" : ""}>{t.label}</a>
          ))}
        </nav>
        <label className="token" title="Admin token unlocks the Review and System tabs">
          <span>{admin === "valid" ? "admin ✓" : admin === "invalid" ? "invalid token" : "admin"}</span>
          <input type="password" value={token} placeholder="token"
                 onChange={(e) => { setToken(e.target.value); adminToken.set(e.target.value); }} />
        </label>
      </header>
      <main key={current.id}>{current.el}</main>
      <footer className="foot">API: <code>{API_BASE}</code> · <a href={`${API_BASE}/docs`} target="_blank" rel="noreferrer">OpenAPI docs</a></footer>
    </div>
  );
}
