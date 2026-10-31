import { Navigate, Route, Routes } from "react-router-dom";
import Layout from "./Layout";
import Queue from "./pages/Queue";
import CaseDetail from "./pages/CaseDetail";
import Store from "./pages/Store";
import Settings from "./pages/Settings";

export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<Navigate to="/queue" replace />} />
        <Route path="/queue" element={<Queue />} />
        <Route path="/cases/:disputeId" element={<CaseDetail />} />
        <Route path="/store" element={<Store />} />
        <Route path="/settings" element={<Settings />} />
        <Route path="*" element={<Navigate to="/queue" replace />} />
      </Route>
    </Routes>
  );
}
