import { Navigate, Route, Routes } from "react-router-dom";
import AlertLine from "./components/AlertLine";
import Header from "./components/Header";
import DiagnosticNav from "./components/DiagnosticNav";
import ObservationPage from "./pages/ObservationPage";
import ReadinessPage from "./pages/ReadinessPage";
import AntennasPage from "./pages/AntennasPage";
import CalBuildPage from "./pages/CalBuildPage";
import CalPage from "./pages/CalPage";
import CandEventPage from "./pages/CandEventPage";
import CandsPage from "./pages/CandsPage";
import EventsPage from "./pages/EventsPage";
import ImagingPage from "./pages/ImagingPage";
import SearchPage from "./pages/SearchPage";
import SnapsPage from "./pages/SnapsPage";
import VisPage from "./pages/VisPage";

export default function App() {
  return (
    <>
      <Header />
      <AlertLine />
      <DiagnosticNav />
      <main className="main">
        <Routes>
          <Route path="/" element={<Navigate to="/observation" replace />} />
          <Route path="/observation" element={<ObservationPage />} />
          <Route path="/readiness" element={<ReadinessPage />} />
          <Route path="/antennas" element={<AntennasPage />} />
          <Route path="/snaps" element={<SnapsPage />} />
          <Route path="/vis" element={<VisPage />} />
          <Route path="/search" element={<SearchPage />} />
          <Route path="/imaging" element={<ImagingPage />} />
          <Route path="/cal" element={<CalPage />} />
          <Route path="/cal/:tag" element={<CalBuildPage />} />
          <Route path="/cands" element={<CandsPage />} />
          <Route path="/cands/:name" element={<CandEventPage />} />
          <Route path="/events" element={<EventsPage />} />
          <Route path="*" element={<Navigate to="/observation" replace />} />
        </Routes>
      </main>
    </>
  );
}
