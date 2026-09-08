import { Navigate, Route, Routes } from "react-router-dom";
import Clock from "./components/Clock";
import StatusStrip from "./components/StatusStrip";
import TabNav from "./components/TabNav";
import ToastStack from "./components/ToastStack";
import EventsPage from "./pages/EventsPage";
import PlaceholderPage from "./pages/PlaceholderPage";

export default function App() {
  return (
    <>
      <header className="app-header">
        <h1>CASM monitor</h1>
        <Clock />
      </header>
      <StatusStrip />
      <TabNav />
      <main className="app-main">
        <Routes>
          <Route path="/" element={<Navigate to="/snaps" replace />} />
          <Route path="/snaps" element={<PlaceholderPage slug="snaps" showSmokeTest />} />
          <Route path="/vis" element={<PlaceholderPage slug="vis" />} />
          <Route path="/search" element={<PlaceholderPage slug="search" />} />
          <Route path="/imaging" element={<PlaceholderPage slug="imaging" />} />
          <Route path="/cal" element={<PlaceholderPage slug="cal" />} />
          <Route path="/cands" element={<PlaceholderPage slug="cands" />} />
          <Route path="/events" element={<EventsPage />} />
          <Route path="*" element={<Navigate to="/snaps" replace />} />
        </Routes>
      </main>
      <ToastStack />
    </>
  );
}
