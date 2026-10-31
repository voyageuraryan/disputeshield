import { lazy, Suspense } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import Layout from "./Layout";
import RequireAuth from "./RequireAuth";

// Pages load on demand: AG Grid (~1 MB) only downloads with the queue, the PayPal SDK only with the store.
const Queue = lazy(() => import("./pages/Queue"));
const CaseDetail = lazy(() => import("./pages/CaseDetail"));
const Store = lazy(() => import("./pages/Store"));
const Settings = lazy(() => import("./pages/Settings"));
const Login = lazy(() => import("./pages/Login"));
const AuthCallback = lazy(() => import("./pages/AuthCallback"));

const Loading = () => <p className="text-sm text-slate-500">Loading…</p>;
const page = (el: React.ReactNode, auth = true) => <Suspense fallback={<Loading />}>{auth ? <RequireAuth>{el}</RequireAuth> : el}</Suspense>;

export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<Navigate to="/queue" replace />} />
        <Route path="/queue" element={page(<Queue />)} />
        <Route path="/cases/:disputeId" element={page(<CaseDetail />)} />
        <Route path="/store" element={page(<Store />, false)} />
        <Route path="/settings" element={page(<Settings />)} />
        <Route path="/login" element={page(<Login />, false)} />
        <Route path="/auth/callback" element={page(<AuthCallback />, false)} />
        <Route path="*" element={<Navigate to="/queue" replace />} />
      </Route>
    </Routes>
  );
}
