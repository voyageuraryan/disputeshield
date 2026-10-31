import { NavLink, Outlet } from "react-router-dom";

const link = ({ isActive }: { isActive: boolean }) =>
  `px-3 py-2 rounded-md text-sm font-medium ${isActive ? "bg-indigo-50 text-indigo-700" : "text-slate-600 hover:bg-slate-100"}`;

export default function Layout() {
  return (
    <div className="min-h-screen bg-slate-50 text-slate-900">
      <header className="border-b bg-white">
        <div className="mx-auto flex max-w-7xl items-center gap-4 px-4 py-3 sm:gap-6">
          <span className="text-lg font-semibold">DisputeShield</span>
          <nav className="flex gap-2">
            <NavLink to="/queue" className={link}>Disputes</NavLink>
            <NavLink to="/store" className={link}>Store</NavLink>
          </nav>
        </div>
      </header>
      <main className="mx-auto max-w-7xl px-3 py-4 sm:px-4 sm:py-6">
        <Outlet />
      </main>
    </div>
  );
}
