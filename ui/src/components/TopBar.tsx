import { useApp } from "../store";

export default function TopBar() {
  const app = useApp();
  const s = app.status;
  const path = app.route.split("?")[0];
  const inAdmin = path.startsWith("/admin");

  let dot = "dot--off";
  let text = "connecting…";
  if (s) {
    if (s.cache.empty) {
      dot = "dot--warn";
      text = "no data yet";
    } else {
      const sc = s.selfcheck;
      const check = !sc ? "self-check not run" : sc.passed ? "self-check passed" : sc.inconclusive ? "self-check inconclusive" : "self-check FAILED";
      dot = !sc || sc.inconclusive ? "dot--warn" : sc.passed ? "" : "dot--crit";
      text = `${s.cache.fixture_data ? "demo data" : s.cache.offline ? "offline cache" : "live cache"} · ${s.n_universe.toLocaleString()} materials · ${check}`;
      if (s.cache.fixture_data) dot = "dot--warn";
    }
  }

  return (
    <header className="topbar">
      <div className="topbar__left">
        <button className="brand" onClick={app.reset} title="New conversation">
          Oxide Triage
        </button>
        <span className="statuspill" title={s?.cache.path}>
          <span className={`dot ${dot}`} />
          <span>{text}</span>
        </span>
      </div>
      <nav className="topbar__right">
        {app.conv && !inAdmin && (
          <button className="navlink" onClick={app.reset}>
            New
          </button>
        )}
        <button className={`navlink ${!inAdmin ? "navlink--active" : ""}`} onClick={() => app.navigate(app.conv ? `/?c=${app.conv.id}` : "/")}>
          Assistant
        </button>
        <button className={`navlink ${inAdmin ? "navlink--active" : ""}`} onClick={() => app.navigate("/admin/profiles")}>
          Admin
        </button>
      </nav>
    </header>
  );
}
