import { useEffect } from "react";
import Admin from "./components/Admin";
import Canvas from "./components/Canvas";
import Chat from "./components/Chat";
import Landing from "./components/Landing";
import TopBar from "./components/TopBar";
import { useApp } from "./store";

export default function App() {
  const app = useApp();
  const path = app.route.split("?")[0];
  const inAdmin = path.startsWith("/admin");
  const inWorkspace = !!app.conv && (app.conv.turns.length > 0 || !!app.live);

  useEffect(() => {
    document.title = inAdmin ? "Admin · Oxide Triage" : app.conv?.title ? `${app.conv.title.slice(0, 40)} · Oxide Triage` : "Oxide Triage";
  }, [inAdmin, app.conv?.title]);

  return (
    <div className={`app ${inWorkspace && !inAdmin ? "app--workspace" : ""}`}>
      <TopBar />
      {app.error && (
        <div className="toast" role="alert">
          <span>{app.error}</span>
          <button className="btn btn--ghost" onClick={app.clearError} aria-label="Dismiss">
            ×
          </button>
        </div>
      )}
      {inAdmin ? (
        <Admin page={path.replace(/^\/admin\/?/, "") || "profiles"} />
      ) : inWorkspace ? (
        <main className="workspace">
          <Chat />
          <Canvas />
        </main>
      ) : (
        <Landing />
      )}
    </div>
  );
}
