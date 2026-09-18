"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { getWorkbenchStatus } from "@/lib/workbench";

export function AppNav() {
  const pathname = usePathname();
  const workbench = pathname.startsWith("/context-workbench");
  const [workbenchLocked, setWorkbenchLocked] = useState(false);
  useEffect(() => {
    let cancelled = false;
    const refresh = async () => {
      try {
        const status = await getWorkbenchStatus();
        if (!cancelled) setWorkbenchLocked(status.activity === "waypoint");
      } catch {
        // Signed-out navigation remains available; the destination owns login.
      }
    };
    void refresh();
    const timer = window.setInterval(() => void refresh(), 5000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, []);
  return <nav className="app-nav" aria-label="Primary">
    <Link href="/" aria-current={workbench ? undefined : "page"}>Waypoint</Link>
    {workbenchLocked && !workbench
      ? <span aria-disabled="true">Context Workbench (locked)</span>
      : <Link href="/context-workbench" aria-current={workbench ? "page" : undefined}>Context Workbench</Link>}
  </nav>;
}
