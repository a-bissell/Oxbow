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
        <button className="brand" onClick={app.reset} title="Oxbow · materials triage. New conversation">
          <svg className="brand__mark" viewBox="0 0 120 120" aria-hidden="true" focusable="false">
            <path d="M 8 98 C 40 98 40 84 60 84 C 80 84 80 98 112 98" fill="none" stroke="var(--accent)" strokeWidth="11" strokeLinecap="round" />
            <path d="M 44 70 A 24 24 0 1 1 76 70" fill="none" stroke="var(--brand-lake)" strokeWidth="11" strokeLinecap="round" />
          </svg>
          <span>Oxbow</span>
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
