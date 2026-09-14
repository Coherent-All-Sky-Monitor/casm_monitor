import { Navigate, Route, Routes } from "react-router-dom";
import AlertLine from "./components/AlertLine";
import Header from "./components/Header";
import DiagnosticNav from "./components/DiagnosticNav";
import OperationsPage from "./pages/OperationsPage";
import SciencePage from "./pages/SciencePage";
import CalibrationComparisonPage from "./pages/CalibrationComparisonPage";
import ReviewPage from "./pages/ReviewPage";
import T1Page from "./pages/T1Page";
import CommissioningPage from "./pages/CommissioningPage";
import SourceHistoryPage from "./pages/SourceHistoryPage";
import SnapWorkspacePage from "./pages/SnapWorkspacePage";
import TransitPage from "./pages/TransitPage";
import ReadinessPage from "./pages/ReadinessPage";
import CandEventPage from "./pages/CandEventPage";
import CandsPage from "./pages/CandsPage";
import EventsPage from "./pages/EventsPage";

export default function App() {
  return (
    <>
      <Header />
      <AlertLine />
      <DiagnosticNav />
      <main className="main">
        <Routes>
          <Route path="/" element={<Navigate to="/observation" replace />} />
          <Route path="/observation" element={<OperationsPage />} />
          <Route path="/readiness" element={<ReadinessPage />} />
          <Route path="/antennas" element={<SnapWorkspacePage />} />
          <Route path="/snaps" element={<SnapWorkspacePage />} />
          <Route path="/vis" element={<SciencePage />} />
          <Route path="/search" element={<T1Page />} />
          <Route path="/review" element={<ReviewPage />} />
          <Route path="/sources" element={<SourceHistoryPage />} />
          <Route path="/cal" element={<CommissioningPage />} />
          <Route path="/cal/compare" element={<CalibrationComparisonPage />} />
          <Route path="/cal/transit" element={<TransitPage />} />
          <Route path="/cal/:tag" element={<CommissioningPage />} />
          <Route path="/cands" element={<CandsPage />} />
          <Route path="/cands/:name" element={<CandEventPage />} />
          <Route path="/events" element={<EventsPage />} />
          <Route path="*" element={<Navigate to="/observation" replace />} />
        </Routes>
      </main>
    </>
  );
}
